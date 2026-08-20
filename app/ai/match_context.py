"""
赛前上下文：历史交锋 / 双方近况 / 伤病 / 阵容 / 球员数据 / 战意 / 天气 / 积分等。

数据来源优先级：
0. DB / Redis 预取缓存
1. AI 搜索（网页搜索 + LLM 结构化，禁止无依据编造）
2. 空结构
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from app.config import settings
from app.services.sports_data import compute_quality

logger = logging.getLogger(__name__)

_EMPTY = {
    "h2h": {"matches": [], "summary": {}, "note": "暂无交锋数据"},
    "home_form": {"matches": [], "summary": {}, "note": "暂无近况数据"},
    "away_form": {"matches": [], "summary": {}, "note": "暂无近况数据"},
    "standings": {"home": None, "away": None, "note": ""},
    "venue": "",
    "dimensions_present": [],
    "dimensions_missing": [
        "h2h",
        "home_form",
        "away_form",
        "standings",
        "analysis",
        "trend",
    ],
    "source": "none",
    "fetched_at": None,
    "model_used": None,
    "cache_hit": False,
    "quality": {"source": "none", "completeness": 0.0, "fields_present": []},
}


def _fixture_key_of(match_info: dict) -> str:
    from app.services.fixture_key import fixture_key

    fk = (match_info.get("fixture_key") or "").strip()
    if fk:
        return fk
    return fixture_key(
        str(match_info.get("sport") or "football"),
        str(match_info.get("home_team") or ""),
        str(match_info.get("away_team") or ""),
    )


def _fixture_key_aliases(match_info: dict) -> list[str]:
    from app.services.fixture_key import fixture_key

    primary = _fixture_key_of(match_info)
    aliases = [primary] if primary else []
    plain = fixture_key(
        str(match_info.get("sport") or "football"),
        str(match_info.get("home_team") or ""),
        str(match_info.get("away_team") or ""),
    )
    if plain and plain not in aliases:
        aliases.append(plain)
    return aliases


def empty_match_context(**extra: Any) -> dict:
    out = dict(_EMPTY)
    out["fetched_at"] = datetime.now(timezone.utc).isoformat()
    out.update(extra)
    out["quality"] = compute_quality(out)
    return out


def _ctx_has_stats(ctx: dict) -> bool:
    h2n = len((ctx.get("h2h") or {}).get("matches") or [])
    hf = len((ctx.get("home_form") or {}).get("matches") or [])
    af = len((ctx.get("away_form") or {}).get("matches") or [])
    standings = ctx.get("standings") or {}
    analysis = ctx.get("analysis") or {}
    trend = ctx.get("trend") or {}
    return bool(
        h2n
        or hf
        or af
        or standings.get("home")
        or standings.get("away")
        or (
            isinstance(analysis, dict)
            and any(analysis.get(k) for k in ("injuries", "features", "compare", "analysis_tables"))
        )
        or (isinstance(trend, dict) and (trend.get("tables") or trend.get("initial_odds")))
    )


def _finalize(ctx: dict) -> dict:
    ctx = dict(ctx or {})
    if "quality" not in ctx or not isinstance(ctx.get("quality"), dict):
        ctx["quality"] = compute_quality(ctx)
    return ctx


async def _scrape_switch_enabled() -> bool:
    """数据源总开关（与前端"爬取数据源开关"同键）。

    关闭（0/false）时：预取循环停 + AI 分析不实时爬取。
    读取失败时保守返回 True（避免 Redis 抖动导致分析缺数据）。
    """
    try:
        from app.core.cache import cache

        val = await cache.get("nowscore:prefetch:enabled")
        if val is not None:
            return str(val) in ("1", "true", "True", "on")
    except Exception:
        pass
    return bool(getattr(settings, "NOWSCORE_PREFETCH_ENABLED", True))


async def _persist(match_info: dict, fixture_key: str, ctx: dict) -> dict:
    from app.services.match_context_store import save_context

    mid = match_info.get("match_id") or match_info.get("id")
    try:
        mid_i = int(mid) if mid is not None else None
    except (TypeError, ValueError):
        mid_i = None
    saved = await save_context(fixture_key=fixture_key, ctx=ctx, match_id=mid_i)
    for alias in _fixture_key_aliases(match_info):
        if alias and alias != fixture_key:
            await save_context(fixture_key=alias, ctx=ctx, match_id=mid_i)
    return saved


async def _load_cached_context(match_info: dict) -> tuple[Optional[dict], str]:
    """只读取 Redis / DB 的缓存上下文，绝不触发网络抓取。"""
    fixture_keys = _fixture_key_aliases(match_info)
    from app.services.match_context_store import load_from_db, load_from_redis

    for fk in fixture_keys:
        try:
            redis_ctx = await load_from_redis(fk)
            if redis_ctx and redis_ctx.get("source") == "nowscore" and _ctx_has_stats(redis_ctx):
                return _finalize(redis_ctx), "redis"
        except Exception as e:
            logger.debug("match_context redis: %s", e)

    for fk in fixture_keys:
        try:
            db_ctx = await load_from_db(fk)
            if db_ctx and db_ctx.get("source") == "nowscore" and _ctx_has_stats(db_ctx):
                return _finalize(db_ctx), "db"
        except Exception as e:
            logger.debug("match_context db: %s", e)

    return None, "miss"


async def fetch_match_context_fast(match_info: dict) -> dict:
    """快路径：只读缓存，Redis/DB 未命中就直接返回空上下文。"""
    if not bool(getattr(settings, "AI_MATCH_CONTEXT_ENABLED", True)):
        return empty_match_context(source="disabled", note="AI_MATCH_CONTEXT_ENABLED=false")

    home = str(match_info.get("home_team") or "").strip()
    away = str(match_info.get("away_team") or "").strip()
    if not home or not away:
        return empty_match_context(note="缺少主客队名")

    cached, source = await _load_cached_context(match_info)
    if cached:
        cached["cache_source"] = source
        cached["context_mode"] = "fast_cache_only"
        return cached
    return empty_match_context(
        source="none",
        note="缓存未命中，已快速跳过抓取",
        context_mode="fast_cache_only",
    )


async def fetch_match_context(match_info: dict, *, refresh_on_miss: bool = True) -> dict:
    """优先读 DB / Redis；默认允许慢速抓取补全，关闭时直接快速跳过。

    数据源开关（nowscore:prefetch:enabled）关闭时不再实时爬取——
    缓存命中照常返回（历史数据仍可用），未命中走快路径。
    """
    if not bool(getattr(settings, "AI_MATCH_CONTEXT_ENABLED", True)):
        return empty_match_context(source="disabled", note="AI_MATCH_CONTEXT_ENABLED=false")

    home = str(match_info.get("home_team") or "").strip()
    away = str(match_info.get("away_team") or "").strip()
    if not home or not away:
        return empty_match_context(note="缺少主客队名")

    fixture_key = _fixture_key_of(match_info)
    cached, _ = await _load_cached_context(match_info)
    if cached:
        return cached
    if not refresh_on_miss:
        return empty_match_context(
            source="none",
            note="缓存未命中，已快速跳过抓取",
            context_mode="fast_cache_only",
        )

    # 数据源开关关闭 → 不实时爬取（缓存未命中即返回空上下文）
    if not await _scrape_switch_enabled():
        return empty_match_context(
            source="none",
            note="数据源开关已关闭，未实时抓取（缓存未命中）",
            context_mode="fast_cache_only",
        )

    search_ctx = None
    # 1. 优先用捷报比分（结构化数据，覆盖8大维度）
    try:
        from app.services.nowscore_scraper import fetch_match_context_via_nowscore
        ns_ctx = await fetch_match_context_via_nowscore(
            home_team=home,
            away_team=away,
            league=str(match_info.get("league") or ""),
            sport=str(match_info.get("sport") or "football"),
        )
        if ns_ctx and _ctx_has_stats(ns_ctx):
            search_ctx = ns_ctx
            logger.info(
                "match_context source=nowscore home=%s away=%s present=%s completeness=%s sid=%s",
                home[:20], away[:20],
                ns_ctx.get("dimensions_present") or [],
                (ns_ctx.get("quality") or {}).get("completeness"),
                ns_ctx.get("schedule_id"),
            )
    except Exception as e:
        logger.warning("nowscore context error: %s", e)

    # nowscore 命中 -> 持久化并返回
    if search_ctx and _ctx_has_stats(search_ctx):
        await _persist(match_info, fixture_key, search_ctx)
        return _finalize(search_ctx)

    # nowscore 未命中时不再降级到 DDG（慢且不可靠），直接返回空上下文
    out = empty_match_context(
        source="none",
        note="捷报比分未匹配到该赛事",
    )
    try:
        from app.services.match_context_store import redis_key
        from app.core.cache import cache as _cache
        ttl = min(settings.AI_CONTEXT_NONE_TTL, int(settings.AI_MATCH_CONTEXT_TTL_SEC))
        await _cache.set_json(redis_key(fixture_key), {**out, "cache_hit": False}, ttl=ttl)
    except Exception:
        pass
    return out
