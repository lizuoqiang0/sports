"""赛前上下文异步预取：AI 搜索，限流。"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import or_, select

from app.config import settings
from app.core.worker_leader import is_background_leader
from app.database import AsyncSessionLocal
from app.models.user import Match, MatchStatus, SportType

logger = logging.getLogger(__name__)

_task: Optional[asyncio.Task] = None
_INTERVAL_SEC = 300
_PREFETCH_WINDOWS_MIN = (90, 60, 30)
_MAX_PER_ROUND = 2
_prefetch_sem = asyncio.Semaphore(1)


def _sport_ok(sport) -> bool:
    s = str(getattr(sport, "value", sport) or "").lower()
    return s in ("football", "soccer", "basketball")


async def prefetch_one_match(match: Match, *, force_lineups: bool = False) -> None:
    from app.ai.match_context import fetch_match_context
    from app.services.fixture_key import fixture_key
    from app.services.match_context_store import load_from_db

    if not _sport_ok(match.sport):
        return

    fk = fixture_key(
        str(getattr(match.sport, "value", match.sport) or "football"),
        match.home_team or "",
        match.away_team or "",
    )
    if not force_lineups:
        existing = await load_from_db(fk)
        if existing and existing.get("source") == "nowscore":
            q = existing.get("quality") or {}
            try:
                if float(q.get("completeness") or 0) >= 0.35:
                    return
            except (TypeError, ValueError):
                pass

    info = {
        "id": match.id,
        "match_id": match.id,
        "home_team": match.home_team,
        "away_team": match.away_team,
        "league": match.league,
        "sport": str(getattr(match.sport, "value", match.sport) or "football").lower(),
        "fixture_key": fk,
        "extra_data": match.extra_data or {},
    }
    async with _prefetch_sem:
        try:
            ctx = await fetch_match_context(info)
            logger.info(
                "prefetch match_id=%s source=%s completeness=%s",
                match.id,
                ctx.get("source"),
                (ctx.get("quality") or {}).get("completeness"),
            )
        except Exception as e:
            logger.warning("prefetch failed match_id=%s: %s", match.id, e)


async def _select_candidates(db) -> list[Match]:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    horizon = now + timedelta(hours=4)
    past = now - timedelta(minutes=20)
    sports = [SportType.FOOTBALL]
    try:
        sports.append(SportType.BASKETBALL)
    except Exception:
        pass
    q = (
        select(Match)
        .where(
            Match.sport.in_(sports),
            or_(
                Match.status == MatchStatus.LIVE,
                Match.status == MatchStatus.UPCOMING,
            ),
            Match.start_time <= horizon,
            Match.start_time >= past,
        )
        .order_by(Match.start_time.asc())
        .limit(_MAX_PER_ROUND)
    )
    return list((await db.execute(q)).scalars().all())


def _near_kickoff_force(start_time: datetime, now: datetime) -> bool:
    if start_time.tzinfo is None:
        st = start_time.replace(tzinfo=timezone.utc)
    else:
        st = start_time
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    mins = (st - now).total_seconds() / 60.0
    for w in _PREFETCH_WINDOWS_MIN:
        if abs(mins - w) <= 8:
            return True
    return 0 < mins <= 35


async def _prefetch_loop() -> None:
    await asyncio.sleep(15)
    while True:
        try:
            if not bool(getattr(settings, "AI_MATCH_CONTEXT_ENABLED", True)):
                await asyncio.sleep(_INTERVAL_SEC)
                continue
            if not is_background_leader():
                await asyncio.sleep(_INTERVAL_SEC)
                continue
            async with AsyncSessionLocal() as db:
                matches = await _select_candidates(db)
                await db.commit()
            now = datetime.now(timezone.utc)
            for m in matches:
                force = _near_kickoff_force(m.start_time, now) if m.start_time else False
                await prefetch_one_match(m, force_lineups=force)
                await asyncio.sleep(2.0)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("context prefetcher loop: %s", e)
        await asyncio.sleep(_INTERVAL_SEC)


def start_context_prefetcher() -> None:
    global _task
    if _task and not _task.done():
        return
    _task = asyncio.create_task(_prefetch_loop(), name="context_prefetcher")
    logger.info("context prefetcher started (interval=%ss max=%s)", _INTERVAL_SEC, _MAX_PER_ROUND)


def stop_context_prefetcher() -> None:
    global _task
    if _task and not _task.done():
        _task.cancel()
    _task = None
