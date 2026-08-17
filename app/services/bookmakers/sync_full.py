"""全量站点同步：默认账户 + 余额/滚球盘口写入。"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
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
from app.services.bookmakers.catalog import BOOKMAKER_CATALOG, provider_name
from app.services.bookmakers.purge import (
    SUPPORTED_SPORTS,
    _is_live_football_basketball,
    purge_demo_matches,
    purge_unsupported_sports,
    purge_virtual_matches,
)
from app.services.bookmakers.match_resolve import _resolve_match_id
from app.services.bookmakers.registry import get_connector
from app.services.bookmakers.odds_write import apply_odds_version
from app.services.bookmakers.sync_session import release_db_session, snapshot_account
from app.services.odds_domain import normalize_odds_data_to_european

logger = logging.getLogger(__name__)

async def ensure_default_accounts(db: AsyncSession, user_id: int) -> list[BookmakerAccount]:
    """为用户确保 OB/平博 配置行存在；线上强制清空演示网址；清除已废弃站点。"""
    from sqlalchemy import delete

    from app.services.bookmakers.plugins.ob.kaiyun import is_demo_url
    from app.services.live_mode import force_live_mode

    # 彻底删除已废弃站点（沙巴 / FB）
    await db.execute(
        delete(BookmakerAccount).where(
            BookmakerAccount.user_id == user_id,
            BookmakerAccount.code.in_(("saba", "fb")),
        )
    )

    result = await db.execute(
        select(BookmakerAccount).where(BookmakerAccount.user_id == user_id)
    )
    existing = {a.code: a for a in result.scalars().all()}
    created = []
    live = force_live_mode()
    for code, meta in BOOKMAKER_CATALOG.items():
        if code in existing:
            acc = existing[code]
            if live and is_demo_url(acc.base_url or ""):
                acc.base_url = ""
                acc.status = BookmakerStatus.DISCONNECTED
                acc.last_error = "已清除演示站地址，请填写真实网址后重新验证"
            created.append(acc)
            continue
        default_url = meta.get("default_url") or ""
        if live and is_demo_url(default_url):
            default_url = ""
        acc = BookmakerAccount(
            user_id=user_id,
            code=code,
            name=meta["name"],
            base_url=default_url,
            username="",
            password_encrypted="",
            session_token_encrypted="",
            profile_json={},
            status=BookmakerStatus.DISCONNECTED,
            balance=Decimal("0"),
            enabled=True,
        )
        db.add(acc)
        created.append(acc)
    await db.flush()
    return created

async def sync_user_bookmakers(db: AsyncSession, user_id: int, *, purge_demo: bool = True) -> dict:
    """同步已连接站点：仅滚球中的足球/篮球盘口 + 余额。"""
    purged = {"deleted_matches": 0}
    if purge_demo:
        purged = await purge_demo_matches(db)
    unsupported = await purge_unsupported_sports(db)
    virtual = await purge_virtual_matches(db)
    purged = {**purged, **unsupported, **virtual}

    result = await db.execute(
        select(BookmakerAccount).where(
            and_(
                BookmakerAccount.user_id == user_id,
                BookmakerAccount.enabled.is_(True),
                BookmakerAccount.status == BookmakerStatus.CONNECTED,
            )
        )
    )
    accounts = list(result.scalars().all())
    if not accounts:
        return {"synced": 0, "message": "没有已连接的站点", "purged_demo": purged}

    # 取本地滚球足球/篮球作为同步基础
    matches_res = await db.execute(
        select(Match)
        .options(selectinload(Match.odds))
        .where(
            Match.status == MatchStatus.LIVE,
            Match.sport.in_(SUPPORTED_SPORTS),
        )
        .order_by(Match.start_time.asc())
        .limit(80)
    )
    matches = list(matches_res.scalars().all())
    local_by_id = {m.id: m for m in matches}

    local_payload = []
    for m in matches:
        base_odds = {"home": 1.90, "away": 2.00}
        for o in m.odds:
            if o.bet_type == BetType.MONEYLINE and o.valid_to is None:
                base_odds = dict(o.odds_data or base_odds)
                break
        local_payload.append({
            "id": m.id,
            "sport": m.sport.value if hasattr(m.sport, "value") else str(m.sport),
            "league": m.league,
            "home_team": m.home_team,
            "away_team": m.away_team,
            "start_time": m.start_time.isoformat() if m.start_time else "",
            "status": m.status.value if hasattr(m.status, "value") else str(m.status),
            "venue": m.venue or "",
            "base_odds": base_odds,
        })

    synced = []
    now = datetime.now(timezone.utc)
    from app.services.bookmakers.registry import is_real_live_account
    from app.database import AsyncSessionLocal
    from app.core.crypto import encrypt_secret

    # 已有任一真实站时，跳过仍指向 .demo 的模拟器赔率
    has_any_live = any(is_real_live_account(a.code, a.base_url or "") for a in accounts)

    jobs: list[dict] = []
    for acc in accounts:
        job = snapshot_account(acc)
        job["is_live"] = is_real_live_account(acc.code, acc.base_url or "")
        jobs.append(job)
    # 释放连接后再打 Gate，避免同步中途 connection is closed
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
            balance = await connector.fetch_balance()
            new_token = getattr(connector, "session_token", None)
            profile = getattr(connector, "_profile", None)

            if has_any_live and not job["is_live"]:
                remote_matches = []
            else:
                fetch_live = getattr(connector, "fetch_live_matches_odds", None)
                if callable(fetch_live):
                    remote_matches = await fetch_live()
                else:
                    remote_matches = await connector.fetch_matches_odds(local_payload)

            deduped: list = []
            seen_ext: set[str] = set()
            for rm in remote_matches or []:
                if not _is_live_football_basketball(rm):
                    continue
                key = rm.external_id or f"{rm.home_team}|{rm.away_team}|{rm.start_time}"
                if key in seen_ext:
                    continue
                seen_ext.add(key)
                deduped.append(rm)
            remote_matches = deduped
            provider = provider_name(acc_code)

            async with AsyncSessionLocal() as wdb:
                matches_res = await wdb.execute(
                    select(Match)
                    .options(selectinload(Match.odds))
                    .where(
                        Match.status == MatchStatus.LIVE,
                        Match.sport.in_(SUPPORTED_SPORTS),
                    )
                    .order_by(Match.start_time.asc())
                    .limit(80)
                )
                local_by_id = {m.id: m for m in matches_res.scalars().all()}
                acc = await wdb.get(BookmakerAccount, acc_id)
                if acc is None:
                    continue

                if new_token and new_token != job["session_token"]:
                    acc.session_token_encrypted = encrypt_secret(new_token)
                if isinstance(profile, dict) and profile:
                    acc.profile_json = profile

                for rm in remote_matches:
                    match_id = await _resolve_match_id(wdb, rm, local_by_id)
                    if match_id is None:
                        continue

                    old = await wdb.execute(
                        select(Odds).where(
                            and_(
                                Odds.match_id == match_id,
                                Odds.provider == provider,
                                Odds.valid_to.is_(None),
                            )
                        )
                    )
                    open_by_bt: dict = {}
                    for row in old.scalars().all():
                        open_by_bt[row.bet_type] = row

                    seen_bt: set = set()
                    for ro in rm.odds_list:
                        try:
                            bt = BetType(ro.bet_type)
                        except ValueError:
                            # 与 sync_live 一致：未知盘口跳过并告警，绝不静默降级 moneyline 污染数据
                            logger.warning(
                                "[sync_full] 未知 bet_type=%s 跳过 match=%s provider=%s",
                                ro.bet_type, match_id, provider,
                            )
                            continue
                        # 同场同盘口只保留一条（多远程场映射到同一 match 时去重）
                        if bt in seen_bt:
                            continue
                        seen_bt.add(bt)
                        new_data = normalize_odds_data_to_european(ro.odds_data)
                        cur = open_by_bt.pop(bt, None)
                        apply_odds_version(
                            wdb,
                            current=cur,
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
                    # 本轮未再出现的盘口关闭
                    for leftover in open_by_bt.values():
                        leftover.valid_to = now

                if remote_matches and acc_code in ("ob", "pinnacle"):
                    from app.services.bookmakers.sport_classify import normalize_sport

                    live_ext = {rm.external_id for rm in remote_matches if rm.external_id}
                    # 本轮实际采到的球类；未采到的球类勿误杀（双采失败时曾把篮球全标完场）
                    scraped_sports = {
                        normalize_sport(getattr(rm, "sport", None))
                        for rm in remote_matches
                    }
                    scraped_sports.discard(None)
                    prefix = f"{acc_code}:%"
                    stale_res = await wdb.execute(
                        select(Match).where(
                            and_(
                                Match.status == MatchStatus.LIVE,
                                Match.sport.in_(SUPPORTED_SPORTS),
                                Match.external_id.like(prefix),
                            )
                        )
                    )
                    for stale in stale_res.scalars().all():
                        if stale.external_id in live_ext:
                            continue
                        stale_sport = normalize_sport(stale.sport)
                        if stale_sport and scraped_sports and stale_sport not in scraped_sports:
                            continue
                        ids = dict((stale.extra_data or {}).get("ids") or {})
                        if len(ids) > 1:
                            stale.status = MatchStatus.UPCOMING
                            stale.end_time = None
                        else:
                            stale.status = MatchStatus.FINISHED
                            if getattr(stale, "end_time", None) is None:
                                stale.end_time = now
                        extra = dict(stale.extra_data or {})
                        extra.pop("clock", None)
                        extra.pop("period", None)
                        stale.extra_data = extra

                if Decimal(str(balance or 0)) > 0:
                    acc.balance = Decimal(str(balance))
                acc.last_sync_at = now.replace(tzinfo=None) if getattr(now, "tzinfo", None) else now
                acc.last_error = None
                acc.status = BookmakerStatus.CONNECTED
                await wdb.commit()
                synced.append({
                    "code": acc_code,
                    "name": job["name"],
                    "balance": float(acc.balance or 0),
                    "matches": len(remote_matches),
                })
        except Exception as e:
            logger.exception("同步失败 %s", acc_code)
            try:
                async with AsyncSessionLocal() as edb:
                    row = await edb.get(BookmakerAccount, acc_id)
                    if row is not None:
                        row.status = BookmakerStatus.ERROR
                        row.last_error = str(e)[:500]
                        await edb.commit()
            except Exception:
                pass

    return {"synced": len(synced), "accounts": synced, "purged_demo": purged}
