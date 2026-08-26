"""
赛事页滚球 → 后台预分析 → AI 投注页读缓存。

任务状态存 Redis，兼容多 uvicorn worker，避免请求内同步 LLM 超时。
OB / 平博同场只分析一次，结果共享；展示时可按站点过滤。
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select

from app.config import settings
from app.core.cache import cache
from app.database import AsyncSessionLocal

logger = logging.getLogger(__name__)

_JOB_TTL = 900  # 15 分钟


def recs_cache_key(user_id: int, sport: str, provider: str = "") -> str:
    sport_n = (sport or "football").strip().lower()
    prov = (provider or "").strip().lower() or "all"
    return f"ai:recs:v3:{int(user_id)}:{sport_n}:{prov}:live"


def _rec_win_rate(rec: dict) -> float:
    """推荐胜率（0–100）。"""
    r = rec.get("recommendation") or {}
    try:
        wr = float(r.get("win_rate"))
        if wr <= 1.0:
            wr *= 100.0
        return wr
    except (TypeError, ValueError):
        pass
    try:
        return float(r.get("confidence") or (rec.get("analysis") or {}).get("confidence") or 0) * 100.0
    except (TypeError, ValueError):
        return 0.0


def filter_recs_by_bet_mode(
    recommendations: list[dict],
    *,
    bet_mode: str = "manual",
    min_confidence: float = settings.AI_MIN_CONFIDENCE,
    min_odds: float = settings.AI_MIN_ODDS,
    max_odds: float | None = settings.AI_MAX_ODDS,
    preferred_sports: list[str] | None = None,
    excluded_teams: list[str] | None = None,
    strat=None,
) -> list[dict]:
    """
    人工 / 自动展示规则一致：仅保留球类/球队/比赛状态过滤。
    高胜率模式是否放行，统一交给「亚洲盘口 + 基本面 + 二次复核」决定。
    """
    from app.ai.analysis_filters import rec_skip_reason
    from app.ai.strategy import StrategyConfig

    _ = bet_mode
    _ = min_confidence
    _ = min_odds
    _ = max_odds
    _ = strat if isinstance(strat, StrategyConfig) else None

    prefs = {
        ("football" if str(x).strip().lower() == "soccer" else str(x).strip().lower())
        for x in (preferred_sports or [])
        if str(x).strip()
    }
    excluded = [str(x).strip().lower() for x in (excluded_teams or []) if str(x).strip()]

    out: list[dict] = []
    for rec in recommendations or []:
        if not isinstance(rec, dict) or rec.get("error"):
            continue
        why = rec_skip_reason(rec)
        if why and why != "odds_out_of_range":
            continue
        sport = str(rec.get("sport") or "").strip().lower()
        sport = "football" if sport == "soccer" else sport
        if prefs and sport not in prefs:
            continue
        teams = f"{str(rec.get('home_team') or '').lower()} {str(rec.get('away_team') or '').lower()}"
        if excluded and any(x in teams for x in excluded):
            continue
        out.append(rec)

    def _sort_key(rec: dict):
        r = rec.get("recommendation") or {}
        return (
            _rec_win_rate(rec),
            1 if r.get("should_bet") else 0,
            float(r.get("odds") or 0),
        )

    out.sort(key=_sort_key, reverse=True)
    return out


def _job_redis_key(user_id: int, sport: str, provider: str = "") -> str:
    # 分析任务始终跨站共享（provider 忽略），避免 OB/平博 tab 各跑一遍 LLM
    return f"ai:recs:job:{recs_cache_key(user_id, sport, '')}"


def _lock_redis_key(user_id: int, sport: str, provider: str = "") -> str:
    return f"ai:recs:lock:{recs_cache_key(user_id, sport, '')}"


def _watch_key(user_id: int, sport: str) -> str:
    sport_n = (sport or "football").strip().lower()
    return f"ai:recs:watch:{int(user_id)}:{sport_n}"


def _cancel_key(user_id: int, sport: str) -> str:
    sport_n = (sport or "football").strip().lower()
    return f"ai:recs:cancel:{int(user_id)}:{sport_n}"


async def is_analysis_watching(user_id: int, sport: str) -> bool:
    try:
        v = await cache.get(_watch_key(user_id, sport))
    except Exception:
        return False
    return str(v or "").strip() in ("1", "true", "yes", "on")


async def list_analysis_watching_sports(user_id: int) -> list[str]:
    sports: list[str] = []
    for sport in ("football", "basketball"):
        if await is_analysis_watching(user_id, sport):
            sports.append(sport)
    return sports


async def _is_cancelled(user_id: int, sport: str) -> bool:
    try:
        v = await cache.get(_cancel_key(user_id, sport))
    except Exception:
        return False
    return str(v or "").strip() in ("1", "true", "yes", "on")


async def start_analysis_watch(
    *,
    user_id: int,
    sport: str,
    limit: int = 80,
) -> dict[str, Any]:
    """开始后台轮询分析（可停止）。"""
    from app.services.bookmakers.sport_classify import normalize_sport

    sport_norm = normalize_sport(sport)
    try:
        await cache.client.delete(_cancel_key(user_id, sport_norm))
        await cache.client.set(_watch_key(user_id, sport_norm), "1", ex=86400)
    except Exception:
        pass
    snap = await ensure_recs_job(
        user_id=user_id,
        sport=sport_norm,
        provider="",
        limit=limit,
        force=True,
    )
    return {
        **snap,
        "analysis_enabled": True,
        "sport": sport_norm,
    }


async def stop_analysis_watch(*, user_id: int, sport: str) -> dict[str, Any]:
    """停止后台分析：清 watch + 置取消标记。"""
    from app.services.bookmakers.sport_classify import normalize_sport

    sport_norm = normalize_sport(sport)
    try:
        await cache.client.set(_watch_key(user_id, sport_norm), "0", ex=86400)
        await cache.client.set(_cancel_key(user_id, sport_norm), "1", ex=900)
    except Exception:
        pass
    await _set_job(
        user_id,
        sport_norm,
        "",
        status="stopped",
        error=None,
    )
    return {
        "status": "stopped",
        "analysis_enabled": False,
        "sport": sport_norm,
        "progress": 0,
        "total": 0,
    }


async def stop_all_analysis_watches(*, user_id: int) -> dict[str, Any]:
    stopped: list[str] = []
    for sport in ("football", "basketball"):
        if await is_analysis_watching(user_id, sport):
            await stop_analysis_watch(user_id=user_id, sport=sport)
            stopped.append(sport)
    return {
        "stopped_sports": stopped,
        "analysis_enabled": False,
    }


async def get_job_snapshot(user_id: int, sport: str, provider: str = "") -> dict[str, Any]:
    _ = provider
    from app.services.bookmakers.sport_classify import normalize_sport

    sport_norm = normalize_sport(sport)
    watching = await is_analysis_watching(user_id, sport_norm)
    try:
        st = await cache.get_json(_job_redis_key(user_id, sport_norm, ""))
    except Exception:
        st = None
    if not isinstance(st, dict):
        return {
            "status": "idle",
            "progress": 0,
            "total": 0,
            "error": None,
            "updated_at": None,
            "started_at": None,
            "analysis_enabled": watching,
        }
    status = st.get("status") or "idle"
    # watch 开启但任务空闲时，对外仍标 idle（由前端/重入队拉起下一轮）
    return {
        "status": status,
        "progress": int(st.get("progress") or 0),
        "total": int(st.get("total") or 0),
        "error": st.get("error"),
        "updated_at": st.get("updated_at"),
        "started_at": st.get("started_at"),
        "token": st.get("token"),
        "analysis_enabled": watching,
    }


async def _set_job(user_id: int, sport: str, provider: str, **fields: Any) -> dict[str, Any]:
    _ = provider
    key = _job_redis_key(user_id, sport, "")
    cur = await cache.get_json(key)
    if not isinstance(cur, dict):
        cur = {}
    cur.update(fields)
    cur["updated_at"] = time.time()
    await cache.set_json(key, cur, ttl=_JOB_TTL)
    return cur


async def list_live_match_ids(
    *,
    sport: str,
    provider: str = "",
    limit: int = 40,
    user_id: int | None = None,
    min_odds: float | None = None,
    max_odds: float | None = None,
) -> list[int]:
    """滚球足球/篮球且有全场大小：跳过超盘/即将结束/赔率区间外；刚开赛优先。"""
    from app.ai.analysis_filters import (
        DEFAULT_MAX_ODDS,
        DEFAULT_MIN_ODDS,
        skip_reason_for_match,
        sort_focused_leagues_first,
        total_odds_meet_min,
    )
    lo = min_odds
    hi = max_odds
    preferred: list[str] = []
    excluded: list[str] = []
    if user_id is not None:
        from app.ai.strategy import load_fresh_strategy

        ai_snap, strat = await load_fresh_strategy(int(user_id))
        lo = float(strat.min_odds) if lo is None else lo
        hi = float(strat.max_odds) if hi is None else hi
        preferred = list(getattr(ai_snap, "preferred_sports", None) or [])
        excluded = list(getattr(ai_snap, "excluded_teams", None) or [])
    else:
        lo = DEFAULT_MIN_ODDS if lo is None else lo
        hi = DEFAULT_MAX_ODDS if hi is None else hi
    lo = float(lo)
    hi = float(hi)

    from app.ai.strategy_gates import sport_is_preferred, team_is_excluded
    from app.models.user import BetType, Match, MatchStatus, Odds, SportType
    from app.services.bookmakers.china_match import is_china_match
    from app.services.bookmakers.plugins.ob.odds import is_virtual_match
    from app.services.bookmakers.sport_classify import normalize_sport
    from app.services.provider_utils import site_code_from_match

    sport_norm = normalize_sport(sport)
    if sport_norm not in ("football", "basketball"):
        return []
    provider_code = (provider or "").strip().lower()
    if provider_code not in ("ob", "pinnacle", ""):
        provider_code = ""
    default_lim = int(getattr(settings, "AI_RECS_LIMIT", 80) or 80)
    limit = max(1, min(int(limit or default_lim), 200))
    now = datetime.now(timezone.utc)

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Match)
            .where(
                Match.sport == SportType(sport_norm),
                Match.status == MatchStatus.LIVE,
                Match.start_time >= now - timedelta(hours=8),
            )
            .order_by(Match.start_time.desc())
            .limit(max(limit * 4, 400))
        )
        rows = list(result.scalars().all())
        by_id: dict[int, Any] = {}
        for m in rows:
            sport_key = m.sport.value if hasattr(m.sport, "value") else str(m.sport)
            if is_virtual_match(sport_key, m.league or "", m.home_team or "", m.away_team or ""):
                continue
            if is_china_match(m.league or "", m.home_team or "", m.away_team or "", sport_key):
                continue
            if not sport_is_preferred(sport_key, preferred):
                continue
            if team_is_excluded(m.home_team or "", m.away_team or "", excluded):
                continue
            site = site_code_from_match(m)
            if provider_code and site != provider_code:
                continue
            if site and site not in ("ob", "pinnacle"):
                continue
            by_id[int(m.id)] = m

        if not by_id:
            await db.commit()
            return []

        from app.ai.analysis_filters import any_market_odds_meet_min

        # 业务只支持全场大小球：候选过滤只看 total/under/over，避免无效调用 LLM。
        odds_res = await db.execute(
            select(Odds.match_id, Odds.bet_type, Odds.total, Odds.spread, Odds.odds_data)
            .where(
                Odds.match_id.in_(list(by_id.keys())),
                Odds.bet_type == BetType.TOTAL,
                Odds.valid_to.is_(None),
                func.coalesce(Odds.last_seen_at, Odds.valid_from) >= (
                    datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5)
                ),
            )
        )
        line_by_mid: dict[int, float] = {}
        odds_by_mid: dict[int, dict] = {}
        with_market: set[int] = set()
        for mid, bet_type, total, spread, odata in odds_res.all():
            mid_i = int(mid)
            bt = bet_type.value if hasattr(bet_type, "value") else str(bet_type)
            with_market.add(mid_i)
            markets = dict((odds_by_mid.get(mid_i) or {}).get("markets") or {})
            flat = {
                k: v
                for k, v in (odds_by_mid.get(mid_i) or {}).items()
                if k != "markets"
            }
            if isinstance(odata, dict):
                entry_odds = {}
                keys = {
                    "total": ("under", "over"),
                    "moneyline": ("home", "away", "draw"),
                    "spread": ("home", "away"),
                }.get(bt, ())
                for k in keys:
                    if odata.get(k) is not None:
                        entry_odds[k] = odata.get(k)
                        flat[k] = odata.get(k)
                if entry_odds:
                    prev = dict(markets.get(bt) or {})
                    prev_odds = dict(prev.get("odds") or {})
                    prev_odds.update(entry_odds)
                    prev["odds"] = prev_odds
                    if bt == "total" and total is not None:
                        try:
                            prev["line"] = float(total)
                            line_by_mid[mid_i] = float(total)
                        except (TypeError, ValueError):
                            pass
                    if bt == "spread" and spread is not None:
                        try:
                            prev["line"] = float(spread)
                        except (TypeError, ValueError):
                            pass
                    markets[bt] = prev
            odds_by_mid[mid_i] = {**flat, "markets": markets}

        kept: list[Any] = []
        skipped = {
            "score_exceeds_line": 0,
            "ending_soon": 0,
            "no_total": 0,
            "odds_out_of_range": 0,
        }
        for mid, m in by_id.items():
            if mid not in with_market:
                skipped["no_total"] += 1
                continue
            omap = odds_by_mid.get(mid) or {}
            sport_key = m.sport.value if hasattr(m.sport, "value") else str(m.sport)
            meet = (
                total_odds_meet_min
                if str(sport_key).lower() == "basketball"
                else any_market_odds_meet_min
            )
            if omap and not meet(omap, floor=lo, ceiling=hi):
                skipped["odds_out_of_range"] += 1
                continue
            why = skip_reason_for_match(
                m,
                line_by_mid.get(mid),
                odds_map=omap or None,
                min_odds=lo,
                max_odds=hi,
            )
            if why:
                skipped[why] = skipped.get(why, 0) + 1
                continue
            kept.append(m)

        kept = sort_focused_leagues_first(kept)
        out = [int(m.id) for m in kept[:limit]]
        if any(skipped.values()):
            logger.info(
                "list_live_match_ids sport=%s kept=%d skip=%s",
                sport_norm,
                len(out),
                {k: v for k, v in skipped.items() if v},
            )
        await db.commit()
        return out


async def ensure_recs_job(
    *,
    user_id: int,
    sport: str,
    provider: str = "",
    limit: int = 40,
    force: bool = False,
) -> dict[str, Any]:
    """跨 worker 安全启动后台分析（跨站共享同一任务）。"""
    from app.services.bookmakers.sport_classify import normalize_sport

    sport_norm = normalize_sport(sport)
    provider_code = (provider or "").strip().lower()
    if provider_code not in ("ob", "pinnacle", ""):
        provider_code = ""

    snap = await get_job_snapshot(user_id, sport_norm, "")
    if not force and snap.get("status") in ("starting", "analyzing"):
        started = float(snap.get("started_at") or 0)
        if started and (time.time() - started) < 720:
            return snap

    lock_key = _lock_redis_key(user_id, sport_norm, "")
    token = uuid.uuid4().hex
    try:
        ok = await cache.client.set(lock_key, token, nx=True, ex=30)
    except Exception:
        ok = False

    if not ok:
        # 锁被他人持有。绝不无条件覆盖（否则多 worker 双跑、LLM 重复消耗）。
        # 仅当快照显示任务僵死（running 超过 30 分钟）时才强制接管。
        cur = await get_job_snapshot(user_id, sport_norm, "")
        started = float(cur.get("started_at") or 0)
        stale = bool(started) and (time.time() - started) > 1800
        if not stale:
            return cur
        try:
            await cache.client.set(lock_key, token, ex=30)
        except Exception:
            pass

    try:
        await cache.client.delete(_cancel_key(user_id, sport_norm))
    except Exception:
        pass

    await _set_job(
        user_id,
        sport_norm,
        "",
        status="starting",
        progress=0,
        total=0,
        error=None,
        started_at=time.time(),
        token=token,
    )
    asyncio.create_task(
        _run_recs_job(
            user_id=user_id,
            sport=sport_norm,
            provider="",  # 始终跨站分析
            limit=limit,
            token=token,
        ),
        name=f"ai-recs-{user_id}-{sport_norm}",
    )
    return await get_job_snapshot(user_id, sport_norm, "")


async def _write_provider_caches(
    user_id: int,
    sport_norm: str,
    all_recs: list[dict],
    *,
    limit: int,
    progress: int,
    total: int,
) -> None:
    """写入 all / ob / pinnacle 三份缓存；all 按同场去重只留一单。"""
    from app.ai.auto_better import pick_best_rec_for_fixture

    by_fixture: dict[str, list[dict]] = {}
    for r in all_recs:
        fk = str(r.get("fixture_key") or r.get("match_id") or "")
        by_fixture.setdefault(fk, []).append(r)

    deduped: list[dict] = []
    for group in by_fixture.values():
        best = pick_best_rec_for_fixture(group)
        if best:
            deduped.append(best)

    def _sort_key(r: dict):
        return (
            1 if r.get("recommendation", {}).get("should_bet") else 0,
            _rec_win_rate(r),
            r.get("recommendation", {}).get("confidence", 0) or 0,
            sum(1 for m in (r.get("markets") or []) if m.get("available")),
        )

    deduped.sort(key=_sort_key, reverse=True)
    all_recs_sorted = sorted(all_recs, key=_sort_key, reverse=True)
    # 缓存保留全部已分析结果；展示层再按人工/自动模式过滤
    cap = max(int(limit or 80), 200)

    analyzed_at = datetime.now(timezone.utc).isoformat()
    for prov, items in (
        ("", deduped[:cap]),
        ("ob", [r for r in all_recs_sorted if str((r.get("recommendation") or {}).get("provider_code") or "").lower() == "ob"][:cap]),
        ("pinnacle", [r for r in all_recs_sorted if str((r.get("recommendation") or {}).get("provider_code") or "").lower() == "pinnacle"][:cap]),
    ):
        payload = {
            "count": len(items),
            "recommendations": items,
            "sport": sport_norm,
            "provider": prov or None,
            "market": "total",
            "scope": "live",
            "status": "ready",
            "progress": progress,
            "total": total,
            "analyzed_at": analyzed_at,
            "source": "matches_live",
            "fixture_dedup": True,
            "raw_count": len(items),
        }
        await cache.set_json(recs_cache_key(user_id, sport_norm, prov), payload, ttl=180)
        if not prov:
            try:
                await cache.set_json(f"ai:recs:{user_id}:{sport_norm}", payload, ttl=180)
            except Exception:
                pass


async def _run_recs_job(
    *,
    user_id: int,
    sport: str,
    provider: str,
    limit: int,
    token: str,
) -> None:
    from app.ai.auto_better import analyze_fixture_group
    from app.core.websocket import manager
    from app.models.user import Match
    from app.services.fixture_key import group_matches_by_fixture

    sport_norm = sport
    _ = provider
    lock_key = _lock_redis_key(user_id, sport_norm, "")

    try:
        # 始终拉双站候选，再按同场分组
        # 尽量覆盖全部滚球同场（上限见 AI_RECS_LIMIT / 200）
        scan_lim = max(int(limit or 80), int(getattr(settings, "AI_RECS_LIMIT", 80) or 80))
        scan_lim = min(scan_lim, 200)
        match_ids = await list_live_match_ids(
            sport=sport_norm, provider="", limit=scan_lim, user_id=user_id
        )
        groups: list[list[int]] = []
        if match_ids:
            async with AsyncSessionLocal() as db:
                rows = list(
                    (await db.execute(select(Match).where(Match.id.in_(match_ids)))).scalars().all()
                )
                await db.commit()
            by_id = {int(m.id): m for m in rows}
            ordered = [by_id[i] for i in match_ids if i in by_id]
            from app.ai.analysis_filters import focused_league_sort_key, sort_focused_leagues_first

            ordered = sort_focused_leagues_first(ordered)
            clustered = group_matches_by_fixture(ordered)
            clustered.sort(
                key=lambda g: min((focused_league_sort_key(x) for x in g), default=(0, 10**9, 0, 0))
            )
            groups = [[int(m.id) for m in g] for g in clustered][:scan_lim]

        await _set_job(
            user_id,
            sport_norm,
            "",
            status="analyzing",
            progress=0,
            total=len(groups),
            error=None,
            token=token,
        )

        if not groups:
            empty_payload = {
                "count": 0,
                "recommendations": [],
                "sport": sport_norm,
                "provider": None,
                "scope": "live",
                "market": "total",
                "status": "ready",
                "progress": 0,
                "total": 0,
                "hint": "暂无滚球大小球（请先在赛事页同步 OB / 平博滚球）",
                "analyzed_at": datetime.now(timezone.utc).isoformat(),
                "source": "matches_live",
                "fixture_dedup": True,
            }
            for prov in ("", "ob", "pinnacle"):
                p = dict(empty_payload)
                p["provider"] = prov or None
                await cache.set_json(recs_cache_key(user_id, sport_norm, prov), p, ttl=180)
            await _set_job(
                user_id, sport_norm, "", status="ready", progress=0, total=0, token=token
            )
            await manager.broadcast_to_user(
                user_id,
                {
                    "type": "ai_recs_ready",
                    "data": {"sport": sport_norm, "provider": None, "count": 0},
                },
            )
            if await is_analysis_watching(user_id, sport_norm):
                asyncio.create_task(
                    _requeue_analysis_round(
                        user_id=user_id,
                        sport=sport_norm,
                        limit=limit,
                    ),
                    name=f"ai-recs-requeue-empty-{user_id}-{sport_norm}",
                )
            return

        from app.ai.strategy import load_fresh_strategy

        _, job_strat = await load_fresh_strategy(user_id)
        # 与自动引擎一致：关闭 LLM 时跳过赛前上下文
        skip_ctx = not bool(getattr(job_strat, "use_llm_analysis", True))

        conc = max(1, int(getattr(settings, "AI_ANALYZE_CONCURRENCY", 2) or 2))
        sem = asyncio.Semaphore(conc)
        all_recs: list[dict] = []
        done = 0
        progress_lock = asyncio.Lock()

        async def _one_group(ids: list[int]):
            nonlocal done
            async with sem:
                if await _is_cancelled(user_id, sport_norm):
                    return []
                try:
                    recs = await analyze_fixture_group(
                        ids, user_id, skip_match_context=skip_ctx
                    )
                except Exception as e:
                    logger.warning("后台同场推荐分析失败 ids=%s: %s", ids, e)
                    recs = []
                async with progress_lock:
                    done += 1
                    cur_done = done
                    for rec in recs or []:
                        if rec and not rec.get("error"):
                            all_recs.append(rec)
                    # 边分析边写缓存，前端可更早看到结果
                    if cur_done == 1 or cur_done % 10 == 0 or cur_done >= len(groups):
                        try:
                            await _write_provider_caches(
                                user_id,
                                sport_norm,
                                list(all_recs),
                                limit=limit,
                                progress=cur_done,
                                total=len(groups),
                            )
                        except Exception:
                            pass
                if cur_done == 1 or cur_done % 5 == 0 or cur_done >= len(groups):
                    await _set_job(
                        user_id,
                        sport_norm,
                        "",
                        status="analyzing",
                        progress=cur_done,
                        total=len(groups),
                        token=token,
                    )
                return recs

        await asyncio.gather(*[_one_group(g) for g in groups])

        if await _is_cancelled(user_id, sport_norm):
            await _set_job(
                user_id,
                sport_norm,
                "",
                status="stopped",
                progress=done,
                total=len(groups),
                token=token,
            )
            logger.info(
                "AI 预分析已停止 user=%s sport=%s progress=%s/%s",
                user_id,
                sport_norm,
                done,
                len(groups),
            )
            return

        await _write_provider_caches(
            user_id,
            sport_norm,
            all_recs,
            limit=limit,
            progress=len(groups),
            total=len(groups),
        )

        # all 缓存条数（去重后）
        all_cached = await cache.get_json(recs_cache_key(user_id, sport_norm, ""))
        n_show = int((all_cached or {}).get("count") or 0)

        await _set_job(
            user_id,
            sport_norm,
            "",
            status="ready",
            progress=len(groups),
            total=len(groups),
            error=None,
            token=token,
        )
        await manager.broadcast_to_user(
            user_id,
            {
                "type": "ai_recs_ready",
                "data": {
                    "sport": sport_norm,
                    "provider": None,
                    "count": n_show,
                    "fixtures": len(groups),
                },
            },
        )
        logger.info(
            "AI 预分析完成 user=%s sport=%s fixtures=%s site_recs=%s",
            user_id,
            sport_norm,
            len(groups),
            len(all_recs),
        )
        # 仍在「开始分析」状态 → 间隔后自动下一轮
        if await is_analysis_watching(user_id, sport_norm):
            asyncio.create_task(
                _requeue_analysis_round(
                    user_id=user_id,
                    sport=sport_norm,
                    limit=limit,
                ),
                name=f"ai-recs-requeue-{user_id}-{sport_norm}",
            )
    except asyncio.CancelledError:
        await _set_job(user_id, sport_norm, "", status="cancelled", token=token)
        raise
    except Exception as e:
        logger.exception("AI 预分析失败 user=%s sport=%s: %s", user_id, sport, e)
        await _set_job(
            user_id, sport_norm, "", status="error", error=str(e), token=token
        )
        if await is_analysis_watching(user_id, sport_norm):
            asyncio.create_task(
                _requeue_analysis_round(
                    user_id=user_id,
                    sport=sport_norm,
                    limit=limit,
                    delay_sec=20,
                ),
                name=f"ai-recs-retry-{user_id}-{sport_norm}",
            )
    finally:
        try:
            cur = await cache.get(lock_key)
            if cur == token:
                await cache.delete(lock_key)
        except Exception:
            pass


async def _requeue_analysis_round(
    *,
    user_id: int,
    sport: str,
    limit: int,
    delay_sec: float | None = None,
) -> None:
    """watch 开启时，一轮结束后自动再跑。"""
    wait = float(
        delay_sec
        if delay_sec is not None
        else getattr(settings, "AI_SCAN_INTERVAL_SEC", 120) or 120
    )
    wait = max(120.0, min(wait, 600.0))
    try:
        await asyncio.sleep(wait)
        if not await is_analysis_watching(user_id, sport):
            return
        if await _is_cancelled(user_id, sport):
            return
        await ensure_recs_job(
            user_id=user_id,
            sport=sport,
            provider="",
            limit=limit,
            force=True,
        )
    except Exception as e:
        logger.warning("requeue analysis failed user=%s sport=%s: %s", user_id, sport, e)
