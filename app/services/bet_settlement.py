"""投注结算服务：按比赛最终比分判定输赢，写回 actual_payout / settled_at。

修复背景：下单时 actual_payout 被直接写成 potential_payout（未结算即记满额赔付），
导致系统无法区分输赢、无法统计真实胜率。本服务：
1. 比赛结束后按 大小球线 vs 全场总得分 判定 赢/输/走水；
2. 赢 → actual_payout = stake * odds；输 → 0；走水 → 退本金 stake；
3. 提供近期真实胜率统计，供策略层自适应调整阈值。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from sqlalchemy import select, func

from app.core.cache import cache
from app.database import AsyncSessionLocal
from app.models.user import Bet, Match, MatchStatus, BetStatus, BetType

logger = logging.getLogger(__name__)

_SETTLED_CACHE_KEY = "bets:stats:recent"
_SETTLED_CACHE_TTL = 300  # 5 分钟

# 比赛完场后多久强制结算（数据源偶发不更新终场比分时的兜底）
_FINALIZE_GRACE_HOURS = 6


def _decide_total_outcome(*, selection: str, line: Optional[float], total: float) -> str:
    """判定大小球单输赢：won / lost / push。"""
    if line is None:
        return "unknown"
    sel = str(selection or "").strip().lower()
    if total > line:
        return "won" if sel == "over" else "lost"
    if total < line:
        return "won" if sel == "under" else "lost"
    return "push"  # 整数线平总得分：走水退本金


async def settle_finished_bets(*, limit: int = 200) -> int:
    """结算所有 已完场 且 未结算 的注单，返回结算条数。"""
    settled = 0
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(Bet, Match)
                .join(Match, Bet.match_id == Match.id)
                .where(
                    Bet.settled_at.is_(None),
                    Bet.status == BetStatus.SUCCESS,
                    Match.status == MatchStatus.FINISHED,
                )
                .order_by(Bet.id.asc())
                .limit(limit)
            )
        ).all()

        if not rows:
            return 0

        for bet, match in rows:
            total = float((match.home_score or 0) + (match.away_score or 0))
            outcome = _decide_total_outcome(
                selection=bet.selection, line=bet.line, total=total
            )
            now = datetime.utcnow()

            if outcome == "won":
                payout = (Decimal(str(bet.stake)) * Decimal(str(bet.odds))).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
            elif outcome == "push":
                payout = Decimal(str(bet.stake)).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
            elif outcome == "lost":
                payout = Decimal("0.00")
            else:  # unknown（缺 line 等）→ 退本金，避免误伤
                payout = Decimal(str(bet.stake)).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )

            bet.actual_payout = payout
            bet.settled_at = now
            settled += 1
            logger.info(
                "[结算] bet=%s %s %s %s line=%s 总得分=%s -> %s 赔付=%s",
                bet.id, match.home_team, match.away_team,
                bet.selection, bet.line, total, outcome, payout,
            )

        await db.commit()

    if settled:
        # 统计口径变了，清掉缓存
        try:
            await cache.delete(_SETTLED_CACHE_KEY)
        except Exception:
            pass
    return settled


async def recent_betting_stats(days: int = 7) -> dict:
    """近 N 天已结算注单的真实胜率统计（含按运动/方向细分）。"""
    try:
        cached = await cache.get_json(_SETTLED_CACHE_KEY)
        if isinstance(cached, dict) and cached.get("days") == days:
            return cached
    except Exception:
        pass

    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(Bet, Match)
                .join(Match, Bet.match_id == Match.id)
                .where(
                    Bet.settled_at.is_not(None),
                    Bet.created_at >= since,
                )
            )
        ).all()

    stats = {
        "days": days,
        "settled": 0,
        "won": 0,
        "lost": 0,
        "push": 0,
        "win_rate": None,
        "stake": 0.0,
        "payout": 0.0,
        "roi": None,
        "by_sport": {},
        "by_selection": {},
    }

    def _bucket(store: dict, key: str) -> dict:
        if key not in store:
            store[key] = {"settled": 0, "won": 0, "lost": 0, "win_rate": None}
        return store[key]

    for bet, match in rows:
        payout = float(bet.actual_payout or 0)
        stake = float(bet.stake or 0)
        stats["settled"] += 1
        stats["stake"] += stake
        stats["payout"] += payout
        if payout > stake + 1e-9:
            stats["won"] += 1
        elif payout < stake - 1e-9:
            stats["lost"] += 1
        else:
            stats["push"] += 1

        sport = str(match.sport.value if hasattr(match.sport, "value") else match.sport or "").lower()
        sb = _bucket(stats["by_sport"], sport or "unknown")
        sb["settled"] += 1
        if payout > stake + 1e-9:
            sb["won"] += 1
        elif payout < stake - 1e-9:
            sb["lost"] += 1

        sel_b = _bucket(stats["by_selection"], str(bet.selection or "").lower())
        sel_b["settled"] += 1
        if payout > stake + 1e-9:
            sel_b["won"] += 1
        elif payout < stake - 1e-9:
            sel_b["lost"] += 1

    decided = stats["won"] + stats["lost"]
    if decided:
        stats["win_rate"] = round(stats["won"] / decided, 4)
    if stats["stake"] > 0:
        stats["roi"] = round((stats["payout"] - stats["stake"]) / stats["stake"], 4)
    for store in (stats["by_sport"], stats["by_selection"]):
        for v in store.values():
            d = v["won"] + v["lost"]
            v["win_rate"] = round(v["won"] / d, 4) if d else None

    try:
        await cache.set_json(_SETTLED_CACHE_KEY, stats, ttl=_SETTLED_CACHE_TTL)
    except Exception:
        pass
    return stats


# ── 周期结算 worker ─────────────────────────────────────────────
_worker_task: Optional[asyncio.Task] = None
_worker_stop = asyncio.Event()


async def _settlement_loop() -> None:
    while not _worker_stop.is_set():
        try:
            n = await settle_finished_bets()
            if n:
                logger.info("[结算worker] 本轮结算 %s 单", n)
        except Exception:
            logger.exception("settlement worker error")
        try:
            await asyncio.wait_for(_worker_stop.wait(), timeout=60.0)
        except asyncio.TimeoutError:
            pass


def start_settlement_worker() -> None:
    global _worker_task
    if _worker_task and not _worker_task.done():
        return
    _worker_stop.clear()
    _worker_task = asyncio.create_task(_settlement_loop(), name="ob-bet-settlement")
    logger.info("bet settlement worker started (interval=60s)")


def stop_settlement_worker() -> None:
    global _worker_task
    _worker_stop.set()
    if _worker_task:
        _worker_task.cancel()
        _worker_task = None
        logger.info("bet settlement worker stopped")
