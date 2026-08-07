"""演示/虚拟/不支持球种等存量清理。"""
from __future__ import annotations

import logging

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import Match, Odds, SportType

logger = logging.getLogger(__name__)

SUPPORTED_SPORTS = (SportType.FOOTBALL, SportType.BASKETBALL)
_SUPPORTED_SPORT_KEYS = {"football", "basketball", "soccer"}

_LAST_VIRTUAL_PURGE_TS: float = 0.0

async def purge_demo_matches(db: AsyncSession) -> dict:
    """删除非 OB 真实源的演示/测试赛事及其关联投注、赔率。"""
    from sqlalchemy import delete, or_

    from app.models.user import Bet

    demo_ids_res = await db.execute(
        select(Match.id).where(
            or_(
                Match.external_id.is_(None),
                and_(
                    ~Match.external_id.like("ob:%"),
                                        ~Match.external_id.like("pinnacle:%"),
                                    ),
            )
        )
    )
    demo_ids = list(demo_ids_res.scalars().all())
    deleted_matches = 0
    if demo_ids:
        await db.execute(delete(Bet).where(Bet.match_id.in_(demo_ids)))
        await db.execute(delete(Match).where(Match.id.in_(demo_ids)))
        deleted_matches = len(demo_ids)

    # 清掉挂在真实赛事上的演示站模拟赔率
    odds_res = await db.execute(
        delete(Odds).where(
            Odds.provider.notin_(["OB体育", "OB Sports", "平博"])
        )
    )
    deleted_odds = odds_res.rowcount or 0
    await db.flush()
    return {"deleted_matches": deleted_matches, "deleted_demo_odds": deleted_odds}



def _is_live_football_basketball(rm) -> bool:
    """只保留已开赛的滚球足球/篮球；今日/早盘/其他球种一律拒绝。"""
    from app.services.bookmakers.match_live import remote_match_started
    from app.services.bookmakers.sport_classify import (
        is_credible_live_basketball,
        normalize_sport,
        reject_sport_mismatch,
    )

    status = str(getattr(rm, "status", "") or "").strip().lower()
    if status not in ("live", "inplay", "in_play", "running", "started"):
        return False
    sport = normalize_sport(getattr(rm, "sport", None))
    if sport not in ("football", "basketball"):
        return False
    league = str(getattr(rm, "league", "") or "").strip()
    if (not league) or league in (
        "未知联赛", "未知", "N/A", "-", "—",
        "足球滚球", "篮球滚球", "滚球", "足球", "篮球", "体育", "今日",
    ):
        return False
    period = str(getattr(rm, "period", "") or "")
    hs = getattr(rm, "home_score", 0) or 0
    aws = getattr(rm, "away_score", 0) or 0
    text = f"{league} {getattr(rm, 'home_team', '')} {getattr(rm, 'away_team', '')}"
    if reject_sport_mismatch(
        sport, period=period, home_score=hs, away_score=aws, text=text
    ):
        return False
    # 篮球必须有真实节次/高分，拒 1-0+进行中 伪滚球
    if sport == "basketball" and not is_credible_live_basketball(
        period=period,
        clock=str(getattr(rm, "clock", "") or ""),
        home_score=hs,
        away_score=aws,
        text=text,
    ):
        return False
    return remote_match_started(rm)


async def purge_unsupported_sports(db: AsyncSession) -> dict:
    """只保留足球 / 篮球，删除其余球种赛事及关联数据。"""
    from sqlalchemy import delete, text

    from app.models.user import Bet

    # PG 枚举名：仅保留 FOOTBALL / BASKETBALL
    ids_res = await db.execute(
        text(
            """
            SELECT id FROM matches
            WHERE sport::text NOT IN ('FOOTBALL', 'BASKETBALL')
            """
        )
    )
    ids = [row[0] for row in ids_res.fetchall()]
    deleted = 0
    if ids:
        await db.execute(delete(Bet).where(Bet.match_id.in_(ids)))
        await db.execute(delete(Odds).where(Odds.match_id.in_(ids)))
        await db.execute(delete(Match).where(Match.id.in_(ids)))
        deleted = len(ids)
        await db.flush()
    mismatched = await purge_sport_mismatches(db)
    unknown = await purge_unknown_leagues(db)
    return {"deleted_unsupported_sports": deleted, **mismatched, **unknown}


async def purge_sport_mismatches(db: AsyncSession) -> dict:
    """删除球类与比分/节次明显矛盾的赛事（如足球却 7:30）。"""
    from sqlalchemy import delete, text

    from app.models.user import Bet
    from app.services.bookmakers.sport_classify import reject_sport_mismatch

    rows = (
        await db.execute(
            text(
                """
                SELECT id, sport::text, COALESCE(home_score,0), COALESCE(away_score,0),
                       COALESCE(extra_data->>'period',''), COALESCE(league,''),
                       COALESCE(home_team,''), COALESCE(away_team,'')
                FROM matches
                WHERE sport::text IN ('FOOTBALL', 'BASKETBALL', 'football', 'basketball')
                """
            )
        )
    ).fetchall()
    bad_ids: list[int] = []
    for row in rows:
        mid, sport_raw, hs, aws, period, league, home, away = row
        sport = (sport_raw or "").lower()
        if sport == "football" or sport_raw == "FOOTBALL":
            declared = "football"
        elif sport == "basketball" or sport_raw == "BASKETBALL":
            declared = "basketball"
        else:
            continue
        if reject_sport_mismatch(
            declared,
            period=str(period or ""),
            home_score=hs,
            away_score=aws,
            text=f"{league} {home} {away}",
        ):
            bad_ids.append(int(mid))
    deleted = 0
    if bad_ids:
        await db.execute(delete(Bet).where(Bet.match_id.in_(bad_ids)))
        await db.execute(delete(Odds).where(Odds.match_id.in_(bad_ids)))
        await db.execute(delete(Match).where(Match.id.in_(bad_ids)))
        deleted = len(bad_ids)
        await db.flush()
        logger.info("purged sport-mismatch matches: %s", deleted)
    return {"deleted_sport_mismatches": deleted}


async def purge_unknown_leagues(db: AsyncSession) -> dict:
    """丢弃「未知联赛」等无有效联赛名的赛事。"""
    from sqlalchemy import delete, or_

    from app.models.user import Bet

    ids_res = await db.execute(
        select(Match.id).where(
            or_(
                Match.league.is_(None),
                Match.league == "",
                Match.league == "未知联赛",
                Match.league == "未知",
                Match.league.in_(
                    [
                        "N/A", "-", "—",
                        "足球滚球", "篮球滚球", "滚球", "足球", "篮球", "体育", "今日",
                    ]
                ),
            )
        )
    )
    ids = list(ids_res.scalars().all())
    deleted = 0
    if ids:
        await db.execute(delete(Bet).where(Bet.match_id.in_(ids)))
        await db.execute(delete(Odds).where(Odds.match_id.in_(ids)))
        await db.execute(delete(Match).where(Match.id.in_(ids)))
        deleted = len(ids)
        await db.flush()
        logger.info("purged unknown-league matches: %s", deleted)
    return {"deleted_unknown_leagues": deleted}


async def purge_virtual_matches(db: AsyncSession) -> dict:
    """严格删除 EAFC / FIFA / VS-虚拟 / NBA2K 等虚拟赛事存量。"""
    from sqlalchemy import delete, text

    from app.models.user import Bet
    from app.services.bookmakers.plugins.ob.odds import is_virtual_match

    # 先用 SQL 粗筛，再在 Python 侧用统一规则确认（避免漏网/误杀边界）
    ids_res = await db.execute(
        text(
            """
            SELECT id, sport::text, league, home_team, away_team
            FROM matches
            WHERE league ILIKE '%EAFC%'
               OR league ILIKE '%EA FC%'
               OR league ILIKE '%FIFA%'
               OR league ILIKE '%电子足球%'
               OR league ILIKE '%电子篮球%'
               OR league ILIKE '%虚拟%'
               OR league ILIKE '%eFootball%'
               OR league ILIKE '%E-Soccer%'
               OR league ILIKE '%Esoccer%'
               OR league ILIKE '%PANDA%'
               OR league ILIKE 'VS-%'
               OR league ILIKE 'VS %'
               OR league ILIKE '%NBA 2K%'
               OR league ILIKE '%NBA2K%'
               OR league ILIKE '%2K2%'
               OR league ILIKE '%FC24%'
               OR league ILIKE '%FC25%'
               OR league ILIKE '%FC26%'
               OR league ILIKE '%瓦尔基里%'
               OR league ILIKE '%瓦尔哈拉%'
               OR league ILIKE '%Valkyrie%'
               OR league ILIKE '%Valhalla%'
               OR league ILIKE '%手柄%'
               OR league ~* '\\(\\s*\\d{1,2}\\s*分钟\\s*\\)'
               OR home_team ILIKE '%EAFC%'
               OR away_team ILIKE '%EAFC%'
               OR home_team ILIKE '%PANDA%'
               OR away_team ILIKE '%PANDA%'
            """
        )
    )
    ids: list[int] = []
    for row in ids_res.fetchall():
        mid, sport, league, home, away = row[0], row[1], row[2] or "", row[3] or "", row[4] or ""
        sport_key = {
            "FOOTBALL": "football",
            "BASKETBALL": "basketball",
        }.get(str(sport), str(sport or "").lower())
        if is_virtual_match(sport_key, league, home, away):
            ids.append(int(mid))

    deleted = 0
    if ids:
        await db.execute(delete(Bet).where(Bet.match_id.in_(ids)))
        await db.execute(delete(Odds).where(Odds.match_id.in_(ids)))
        await db.execute(delete(Match).where(Match.id.in_(ids)))
        deleted = len(ids)
        await db.flush()
    return {"deleted_virtual_matches": deleted}


async def purge_bookmaker_matches(db: AsyncSession, site_code: str) -> dict:
    """删除指定站点全部赛事/赔率（及关联注单），便于重新拉取写入。"""
    from sqlalchemy import delete

    from app.models.user import Bet
    from app.services.bookmakers.catalog import provider_name

    code = (site_code or "").strip().lower()
    if code not in ("ob", "pinnacle"):
        return {"deleted_matches": 0, "deleted_odds": 0, "deleted_bets": 0, "site": code}

    prefix = f"{code}:"
    provider = provider_name(code)
    # 兼容历史 provider 文案
    provider_aliases = {provider, code, code.upper()}
    if code == "pinnacle":
        provider_aliases.update({"平博", "Pinnacle", "PINNACLE"})
    elif code == "ob":
        provider_aliases.update({"OB体育", "OB Sports", "OB", "开云"})

    ids_res = await db.execute(
        select(Match.id).where(Match.external_id.like(f"{prefix}%"))
    )
    ids = list(ids_res.scalars().all())
    deleted_bets = 0
    deleted_odds = 0
    deleted_matches = 0
    if ids:
        br = await db.execute(delete(Bet).where(Bet.match_id.in_(ids)))
        deleted_bets = int(br.rowcount or 0)
        od = await db.execute(delete(Odds).where(Odds.match_id.in_(ids)))
        deleted_odds = int(od.rowcount or 0)
        mr = await db.execute(delete(Match).where(Match.id.in_(ids)))
        deleted_matches = int(mr.rowcount or 0)

    # 清掉仍挂在其他场次上的该站赔率
    aliases = [p for p in provider_aliases if p]
    if aliases:
        od2 = await db.execute(delete(Odds).where(Odds.provider.in_(list(aliases))))
        deleted_odds += int(od2.rowcount or 0)
    await db.flush()
    logger.info(
        "purged site=%s matches=%s odds=%s bets=%s",
        code,
        deleted_matches,
        deleted_odds,
        deleted_bets,
    )
    return {
        "site": code,
        "deleted_matches": deleted_matches,
        "deleted_odds": deleted_odds,
        "deleted_bets": deleted_bets,
    }


async def clear_ai_recs_cache(*, site_code: str = "") -> int:
    """清理 AI 推荐相关 Redis 缓存。"""
    from app.core.cache import cache

    code = (site_code or "").strip().lower()
    n = 0
    try:
        client = cache.client
    except Exception:
        return 0
    try:
        async for key in client.scan_iter(match="ai:recs*", count=300):
            k = key.decode() if isinstance(key, (bytes, bytearray)) else str(key)
            if code and code not in k:
                # 仍删除未带站点后缀的聚合缓存
                if not (k.count(":") <= 4 and ("football" in k or "basketball" in k)):
                    continue
            await client.delete(k)
            n += 1
    except Exception as e:
        logger.warning("clear_ai_recs_cache failed: %s", e)
    return n


async def maybe_run_periodic_purge(*, min_interval_sec: float = 300.0) -> dict:
    """
    低频清理（独立短会话）。勿在 sync_live 热路径内联调用。
    默认最多每 5 分钟一次。
    """
    import time

    global _LAST_VIRTUAL_PURGE_TS
    now_ts = time.time()
    if now_ts - float(_LAST_VIRTUAL_PURGE_TS or 0) < min_interval_sec:
        return {}
    from app.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        unsupported = await purge_unsupported_sports(db)
        virtual = await purge_virtual_matches(db)
        await db.commit()
    _LAST_VIRTUAL_PURGE_TS = now_ts
    out = {**unsupported, **virtual}
    if any(out.values()):
        logger.info("periodic purge: %s", out)
    return out
