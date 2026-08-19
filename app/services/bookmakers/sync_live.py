"""轻量滚球同步：比分/时钟/赔率 + WebSocket 推送。"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.user import (
    BookmakerAccount,
    BookmakerStatus,
    BetType,
    Match,
    MatchStatus,
    Odds,
)
from app.services.bookmakers.catalog import provider_name
from app.services.bookmakers.match_resolve import _apply_score_clock, _resolve_match_id
from app.services.bookmakers.purge import SUPPORTED_SPORTS, _is_live_football_basketball
from app.services.bookmakers.registry import get_connector
from app.services.bookmakers.sync_session import release_db_session, snapshot_account
from app.services.bookmakers.odds_write import apply_odds_version, public_odds_data
from app.services.odds_domain import normalize_odds_data_to_european

logger = logging.getLogger(__name__)


def _public_odds_data(odds_data: dict | None) -> dict:
    """去掉内部 _ob 引用，供前端/WS 展示。"""
    return public_odds_data(odds_data)


async def _reload_live_map(session: AsyncSession) -> dict[int, Match]:
    matches_res = await session.execute(
        select(Match)
        .options(selectinload(Match.odds))
        .where(
            Match.status == MatchStatus.LIVE,
            Match.sport.in_(SUPPORTED_SPORTS),
        )
        .order_by(Match.updated_at.desc())
        .limit(800)
    )
    return {m.id: m for m in matches_res.scalars().all()}


async def _broadcast_match_update(match: Match, odds_payload: list[dict], now: datetime) -> None:
    """同 tick 合并推送：live 频道 + match 频道各一条，避免 moneyline 重复风暴。"""
    from app.core.websocket import manager, WSEventType

    extra = match.extra_data or {}
    data = {
        "match_id": match.id,
        "home_team": match.home_team,
        "away_team": match.away_team,
        "home_score": match.home_score,
        "away_score": match.away_score,
        "clock": extra.get("clock") or "",
        "period": extra.get("period") or "",
        "status": match.status.value if hasattr(match.status, "value") else str(match.status),
        "sport": match.sport.value if hasattr(match.sport, "value") else str(match.sport),
        "league": match.league,
        "odds": odds_payload,
        "timestamp": now.isoformat(),
    }
    msg = {"type": "match_update", "channel": "odds:live", "data": data}
    await manager.broadcast_to_channel("odds:live", msg)
    await manager.broadcast_to_channel(
        f"odds:match:{match.id}",
        {
            **msg,
            "type": WSEventType.ODDS_UPDATE if odds_payload else WSEventType.MATCH_STATUS,
            "channel": f"odds:match:{match.id}",
        },
    )


async def sync_live_scores_odds(
    db: AsyncSession,
    user_id: int | None = None,
    *,
    only_account_id: int | None = None,
    refresh_balance: bool = True,
) -> dict:
    """
    轻量滚球同步：仅足球/篮球 LIVE 比分/时钟/赔率，并通过 WebSocket 推送。
    user_id 为空时，同步所有已连接真站账户（按 base_url+username 去重）。
    only_account_id: 仅同步指定账户。
    refresh_balance: 是否顺带刮余额（轮询可关，减少与盘口抢车道）。

    注意：purge 不在本热路径执行（见 sync_full / purge.maybe_run_periodic_purge）。
    """
    from app.core.cache import cache
    from app.database import AsyncSessionLocal
    from app.services.bookmakers.registry import is_real_live_account

    filters = [
        BookmakerAccount.enabled.is_(True),
        BookmakerAccount.status == BookmakerStatus.CONNECTED,
        BookmakerAccount.code.in_(["ob", "pinnacle"]),
    ]
    if user_id is not None:
        filters.append(BookmakerAccount.user_id == user_id)
    if only_account_id is not None:
        filters.append(BookmakerAccount.id == int(only_account_id))

    result = await db.execute(select(BookmakerAccount).where(and_(*filters)))
    accounts = [
        a for a in result.scalars().all() if is_real_live_account(a.code, a.base_url or "")
    ]
    if not accounts:
        return {"updated": 0, "matches": 0, "message": "无已连接真站"}

    # 同一 base_url+username 只拉一次
    seen_keys: set[str] = set()
    unique_accounts: list[BookmakerAccount] = []
    for acc in accounts:
        key = f"{acc.base_url}|{acc.username}"
        if key in seen_keys:
            continue
        seen_keys.add(key)
        unique_accounts.append(acc)

    now = datetime.now(timezone.utc)
    updated = 0
    total_remote = 0

    jobs = [snapshot_account(acc) for acc in unique_accounts]
    await release_db_session(db)

    for job in jobs:
        acc_id = job["id"]
        acc_code = job["code"]
        try:
            connector = get_connector(
                acc_code,
                base_url=job["base_url"],
                username=job["username"],
                password=job["password"],
                balance=job["balance"],
                session_token=job["session_token"],
                profile=job["profile"],
            )
            fetch_live = getattr(connector, "fetch_live_matches_odds", None)
            if not callable(fetch_live):
                continue
            # 无 DB 会话占用期间调用 Gate
            remote_matches = await fetch_live()
            bal_val = None
            if refresh_balance:
                try:
                    bal = await connector.fetch_balance()
                    if bal and bal > 0:
                        bal_val = Decimal(bal)
                except Exception:
                    pass

            remote_matches = [rm for rm in (remote_matches or []) if _is_live_football_basketball(rm)]
            deduped: list = []
            seen_ext: set[str] = set()
            for rm in remote_matches:
                key = rm.external_id or f"{rm.home_team}|{rm.away_team}|{rm.start_time}"
                if key in seen_ext:
                    continue
                seen_ext.add(key)
                deduped.append(rm)
            remote_matches = deduped
            total_remote += len(remote_matches)
            provider = provider_name(acc_code)
            written_keys: set[tuple] = set()

            # 每站独立短会话写库，避免跨站污染 / 断连
            async with AsyncSessionLocal() as wdb:
                local_by_id = await _reload_live_map(wdb)
                acc = await wdb.get(BookmakerAccount, acc_id)
                if acc is None:
                    continue
                if bal_val is not None:
                    acc.balance = bal_val

                for rm in remote_matches:
                    match_id = await _resolve_match_id(wdb, rm, local_by_id)
                    if match_id is None:
                        continue
                    match = local_by_id.get(match_id)
                    if match is None:
                        found = await wdb.execute(
                            select(Match).options(selectinload(Match.odds)).where(Match.id == match_id)
                        )
                        match = found.scalar_one_or_none()
                        if match:
                            local_by_id[match.id] = match
                    if not match:
                        continue

                    await _apply_score_clock(match, rm)
                    if rm.status == "live":
                        match.status = MatchStatus.LIVE
                    match.updated_at = now.replace(tzinfo=None) if getattr(now, "tzinfo", None) else now

                    odds_payload = []
                    for ro in rm.odds_list:
                        try:
                            bt = BetType(ro.bet_type)
                        except ValueError:
                            bt = BetType.MONEYLINE

                        write_key = (match_id, provider, bt.value if hasattr(bt, "value") else str(bt))
                        if write_key in written_keys:
                            continue

                        current = None
                        for o in (match.odds or []):
                            if o.valid_to is None and o.provider == provider and o.bet_type == bt:
                                current = o
                                break

                        pub = _public_odds_data(ro.odds_data)
                        new_data = normalize_odds_data_to_european(ro.odds_data)
                        _, wrote = apply_odds_version(
                            wdb,
                            current=current,
                            match_id=match_id,
                            bet_type=bt,
                            odds_data=new_data,
                            spread=ro.spread,
                            total=ro.total,
                            provider=provider,
                            is_live=True,
                            now=now,
                            odds_cls=Odds,
                        )
                        if wrote:
                            written_keys.add(write_key)
                            updated += 1

                        odds_payload.append({
                            "bet_type": bt.value if hasattr(bt, "value") else str(bt),
                            "odds_data": pub,
                            "spread": ro.spread,
                            "total": ro.total,
                            "is_live": True,
                        })

                    await _broadcast_match_update(match, odds_payload, now)

                acc.last_sync_at = now.replace(tzinfo=None) if getattr(now, "tzinfo", None) else now
                acc.last_error = None
                await wdb.commit()
        except Exception as e:
            err_txt = str(e)
            logger.exception("live sync failed %s", acc_code)
            try:
                async with AsyncSessionLocal() as edb:
                    row = await edb.get(BookmakerAccount, acc_id)
                    if row is not None:
                        row.last_error = err_txt[:500]
                        await edb.commit()
            except Exception:
                logger.debug("failed to persist last_error for %s", acc_code, exc_info=True)
            continue

    # 超过 20 分钟未更新的 LIVE 降级；未真正开赛的 LIVE 降为未开始；伪篮球直接完场
    try:
        from app.services.bookmakers.match_live import local_match_started
        from app.services.bookmakers.sport_classify import is_credible_live_basketball

        cutoff = (now.replace(tzinfo=None) if getattr(now, "tzinfo", None) else now) - timedelta(minutes=20)
        async with AsyncSessionLocal() as ddb:
            live_q = await ddb.execute(
                select(Match).where(
                    Match.status == MatchStatus.LIVE,
                    Match.sport.in_(SUPPORTED_SPORTS),
                )
            )
            for row in live_q.scalars().all():
                sport_key = (
                    row.sport.value if hasattr(row.sport, "value") else str(row.sport or "")
                ).lower()
                extra = dict(row.extra_data or {})
                if sport_key == "basketball" and not is_credible_live_basketball(
                    period=str(extra.get("period") or ""),
                    clock=str(extra.get("clock") or ""),
                    home_score=row.home_score or 0,
                    away_score=row.away_score or 0,
                    text=f"{row.league or ''} {row.home_team or ''} {row.away_team or ''}",
                ):
                    row.status = MatchStatus.FINISHED
                    if getattr(row, "end_time", None) is None:
                        row.end_time = now
                    extra.pop("clock", None)
                    extra.pop("period", None)
                    row.extra_data = extra
                    continue
                if not local_match_started(row, now=now if getattr(now, "tzinfo", None) else now.replace(tzinfo=timezone.utc)):
                    row.status = MatchStatus.UPCOMING
                    row.end_time = None
                    extra = dict(row.extra_data or {})
                    extra.pop("clock", None)
                    extra.pop("period", None)
                    row.extra_data = extra
                    continue
                ua = row.updated_at
                if ua is not None:
                    ua_naive = ua.replace(tzinfo=None) if getattr(ua, "tzinfo", None) else ua
                    if ua_naive < cutoff:
                        row.status = MatchStatus.FINISHED
                        if getattr(row, "end_time", None) is None:
                            row.end_time = now
                        extra = dict(row.extra_data or {})
                        extra.pop("clock", None)
                        extra.pop("period", None)
                        row.extra_data = extra
            await ddb.commit()
    except Exception:
        logger.debug("stale/not-started live demote failed", exc_info=True)

    # 清列表缓存，下一轮 HTTP 拉到新比分
    try:
        keys = await cache.client.keys("matches:list:*")
        if keys:
            await cache.client.delete(*keys)
    except Exception:
        pass

    # 异步预取赛前上下文（串行限流，每轮最多 1 场）
    try:
        import asyncio

        from app.models.user import SportType
        from app.services.context_prefetcher import prefetch_one_match

        async with AsyncSessionLocal() as pdb:
            rows = (
                await pdb.execute(
                    select(Match)
                    .where(
                        Match.status.in_((MatchStatus.LIVE, MatchStatus.UPCOMING)),
                        Match.sport.in_((SportType.FOOTBALL, SportType.BASKETBALL)),
                    )
                    .order_by(Match.start_time.asc())
                    .limit(1)
                )
            ).scalars().all()
            await pdb.commit()
        for m in rows:
            asyncio.create_task(prefetch_one_match(m))
    except Exception:
        logger.debug("context prefetch schedule skipped", exc_info=True)

    return {
        "updated": updated,
        "matches": total_remote,
        "accounts": len(unique_accounts),
    }
