"""赛前上下文持久化：DB + Redis，供预取与 AI 读取。"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select

from app.config import settings
from app.core.cache import cache
from app.database import AsyncSessionLocal
from app.models.user import MatchContextRow
from app.services.sports_data import compute_quality

logger = logging.getLogger(__name__)

CACHE_PREFIX = "ai:match_ctx:v6:"
TRACKED_DIMENSIONS = (
    "h2h",
    "home_form",
    "away_form",
    "standings",
    "analysis",
    "trend",
)


def redis_key(fixture_key: str) -> str:
    return f"{CACHE_PREFIX}{fixture_key}"


def _sync_dimensions(ctx: dict, quality: dict) -> dict:
    fields = [
        str(x).strip()
        for x in (quality.get("fields_present") or [])
        if str(x).strip() in TRACKED_DIMENSIONS
    ]
    seen: set[str] = set()
    present: list[str] = []
    for field in fields:
        if field not in seen:
            seen.add(field)
            present.append(field)
    missing = [field for field in TRACKED_DIMENSIONS if field not in seen]
    return {
        **ctx,
        "dimensions_present": present,
        "dimensions_missing": missing,
        "quality": quality,
    }


def ensure_quality(ctx: dict) -> dict:
    if not isinstance(ctx, dict):
        return {}
    q = ctx.get("quality")
    if not isinstance(q, dict) or "completeness" not in q or "fields_present" not in q:
        q = compute_quality(ctx)
    return _sync_dimensions(dict(ctx), q)


async def load_from_redis(fixture_key: str) -> Optional[dict]:
    if not fixture_key:
        return None
    try:
        cached = await cache.get_json(redis_key(fixture_key))
        if isinstance(cached, dict) and cached.get("source") in ("nowscore", "none"):
            return ensure_quality({**cached, "cache_hit": True})
    except Exception as e:
        logger.debug("match_ctx redis load: %s", e)
    return None


async def load_from_db(fixture_key: str) -> Optional[dict]:
    if not fixture_key:
        return None
    try:
        async with AsyncSessionLocal() as db:
            row = (
                await db.execute(
                    select(MatchContextRow).where(MatchContextRow.fixture_key == fixture_key)
                )
            ).scalar_one_or_none()
            # 精确匹配未命中，尝试模糊匹配（去掉 bucket 后缀如 :82694）
            if not row:
                row = (
                    await db.execute(
                        select(MatchContextRow).where(
                            MatchContextRow.fixture_key.like(fixture_key + ":%")
                        )
                    )
                ).scalar_one_or_none()
            await db.commit()
            if not row:
                return None
            now = datetime.now(timezone.utc)
            exp = row.expires_at
            if exp is not None:
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                if exp < now:
                    return None
            payload = dict(row.payload or {})
            payload["source"] = row.source or payload.get("source") or "none"
            payload["quality"] = row.quality or payload.get("quality") or compute_quality(payload)
            payload["fetched_at"] = (
                row.fetched_at.isoformat() if row.fetched_at else payload.get("fetched_at")
            )
            payload["cache_hit"] = True
            payload["from_db"] = True
            return ensure_quality(payload)
    except Exception as e:
        logger.debug("match_ctx db load: %s", e)
    return None


async def save_context(
    *,
    fixture_key: str,
    ctx: dict,
    match_id: Optional[int] = None,
    ttl_sec: Optional[int] = None,
) -> dict:
    """写入 DB + Redis。"""
    ctx = ensure_quality(dict(ctx or {}))
    source = str(ctx.get("source") or "none")
    quality = ctx.get("quality") if isinstance(ctx.get("quality"), dict) else compute_quality(ctx)
    ctx["quality"] = quality
    ttl = int(ttl_sec or getattr(settings, "AI_MATCH_CONTEXT_TTL_SEC", 21600) or 21600)
    if source == "none":
        ttl = min(settings.AI_CONTEXT_NONE_TTL, ttl)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=ttl)

    try:
        async with AsyncSessionLocal() as db:
            row = (
                await db.execute(
                    select(MatchContextRow).where(MatchContextRow.fixture_key == fixture_key)
                )
            ).scalar_one_or_none()
            if row:
                row.source = source
                row.payload = ctx
                row.quality = quality
                row.fetched_at = now.replace(tzinfo=None) if now.tzinfo else now
                row.expires_at = expires.replace(tzinfo=None) if expires.tzinfo else expires
                if match_id:
                    row.match_id = match_id
            else:
                db.add(
                    MatchContextRow(
                        match_id=match_id,
                        fixture_key=fixture_key,
                        source=source,
                        payload=ctx,
                        quality=quality,
                        fetched_at=now,
                        expires_at=expires,
                    )
                )
            await db.commit()
    except Exception as e:
        logger.warning("match_ctx db save failed: %s", e)

    try:
        await cache.set_json(redis_key(fixture_key), {**ctx, "cache_hit": False}, ttl=ttl)
    except Exception as e:
        logger.debug("match_ctx redis save: %s", e)
    return ctx
