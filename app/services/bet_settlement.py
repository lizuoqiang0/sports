"""投注结算服务：按比赛最终比分判定输赢，写回 actual_payout / settled_at。

修复背景：下单时 actual_payout 被直接写成 potential_payout（未结算即记满额赔付），
导致系统无法区分输赢、无法统计真实胜率。本服务：
1. 比赛结束后按 小球线 vs 全场总得分 判定 赢/输/走水；
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


def _decide_total_outcome(*, selection: str, line: Optional[float], total: float) -> str:
    """判定小球单输赢：won / lost / push。"""
    if line is None:
        return "unknown"
    sel = str(selection or "").strip().lower()
    if total > line:
        return "lost"
    if total < line:
        return "won" if sel == "under" else "lost"
    return "push"  # 整数线平总得分：走水退本金


def _quarter_split(line: float) -> Optional[tuple[float, float]]:
    """四分之一盘（x.25 / x.75）拆成相邻两条半线。

    例：2.75 → (2.5, 3.0)，各押半注；2.25 → (2.0, 2.5)。
    整数线 / 半球线（x.0 / x.5）返回 None（单线结算）。
    """
    try:
        q = round(float(line) * 4)
    except (TypeError, ValueError, OverflowError):
        return None
    if q % 2 == 1:
        return (float(line) - 0.25, float(line) + 0.25)
    return None


async def settle_finished_bets(*, limit: int = 200) -> int:
    """结算所有 已完场/已取消 且 未结算 的注单，返回结算条数。"""
    settled = 0
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(Bet, Match)
                .join(Match, Bet.match_id == Match.id)
                .where(
                    Bet.settled_at.is_(None),
                    Bet.status == BetStatus.SUCCESS,
                    Bet.bet_type == BetType.TOTAL,  # 仅小球参与比分结算
                    Match.status.in_(
                        [MatchStatus.FINISHED, MatchStatus.CANCELLED, MatchStatus.POSTPONED]
                    ),
                )
                .order_by(Bet.id.asc())
                .limit(limit)
            )
        ).all()

        if not rows:
            return 0

        now = datetime.utcnow()
        for bet, match in rows:
            stake_d = Decimal(str(bet.stake)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            m_status = match.status

            # 取消/延期比赛：全额退本金（void）
            if m_status in (MatchStatus.CANCELLED, MatchStatus.POSTPONED):
                payout = stake_d
                outcome = "void"
            else:
                total = float((match.home_score or 0) + (match.away_score or 0))
                line_f = None
                try:
                    if bet.line is not None:
                        line_f = float(bet.line)
                except (TypeError, ValueError):
                    line_f = None

                if line_f is None or line_f <= 0:
                    # line 缺失或 0.0（解析失败下毒）→ 无法判定，退本金
                    payout = stake_d
                    outcome = "unknown"
                    logger.warning(
                        "[结算] bet=%s line无效(%s)，退本金处理", bet.id, bet.line,
                    )
                else:
                    sport = str(
                        match.sport.value if hasattr(match.sport, "value") else match.sport or ""
                    ).lower()
                    # 篮球比分可信度防护：终场总分 < 50%盘口线 → 大概率被冻结在
                    # 中途比分（伪完场/超时完场），退本金避免误判
                    if sport == "basketball" and total < line_f * 0.5:
                        payout = stake_d
                        outcome = "unknown"
                        logger.warning(
                            "[结算] bet=%s 篮球总分%g异常低于线%.1f（疑似中途冻结比分），退本金",
                            bet.id, total, line_f,
                        )
                    else:
                        lines = _quarter_split(line_f) or (line_f,)
                        half_stake = (stake_d / len(lines)).quantize(
                            Decimal("0.01"), rounding=ROUND_HALF_UP
                        )
                        payout = Decimal("0.00")
                        parts = []
                        for ln in lines:
                            oc = _decide_total_outcome(
                                selection=bet.selection, line=ln, total=total
                            )
                            if oc == "won":
                                payout += (half_stake * Decimal(str(bet.odds))).quantize(
                                    Decimal("0.01"), rounding=ROUND_HALF_UP
                                )
                            elif oc in ("push", "unknown"):
                                payout += half_stake
                            parts.append(oc)
                        outcome = "/".join(parts) if len(parts) > 1 else parts[0]

            bet.actual_payout = payout
            bet.settled_at = now
            settled += 1
            logger.info(
                "[结算] bet=%s %s %s %s line=%s 总得分=%s -> %s 赔付=%s",
                bet.id, match.home_team, match.away_team,
                bet.selection, bet.line,
                (match.home_score or 0) + (match.away_score or 0) if m_status == MatchStatus.FINISHED else "-",
                outcome, payout,
            )

        await db.commit()

    if settled:
        # 统计口径变了，清掉缓存（全局 + 本批涉及用户）
        try:
            await cache.delete(_SETTLED_CACHE_KEY)
            touched_users = {b.user_id for b, _ in rows if getattr(b, "user_id", None)}
            for uid in touched_users:
                await cache.delete(f"{_SETTLED_CACHE_KEY}:{uid}")
        except Exception:
            pass
    return settled


async def recent_betting_stats(
    days: int = 7,
    user_id: Optional[int] = None,
    ai_only: bool = True,
) -> dict:
    """近 N 天已结算注单的真实胜率统计（含按运动/方向细分）。

    user_id：按用户隔离（AI 自适应门槛不得跨用户污染）。
    ai_only：仅统计 AI 注单（人工单不参与 AI 门槛自适应）。
    """
    cache_key = f"{_SETTLED_CACHE_KEY}:{user_id}" if user_id else _SETTLED_CACHE_KEY
    try:
        cached = await cache.get_json(cache_key)
        if isinstance(cached, dict) and cached.get("days") == days and cached.get("user_id") == user_id:
            return cached
    except Exception:
        pass

    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    conditions = [
        Bet.settled_at.is_not(None),
        Bet.created_at >= since,
    ]
    if user_id is not None:
        conditions.append(Bet.user_id == int(user_id))
    if ai_only:
        conditions.append(Bet.is_ai_bet.is_(True))
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(Bet, Match)
                .join(Match, Bet.match_id == Match.id)
                .where(*conditions)
            )
        ).all()

    stats = {
        "days": days,
        "user_id": user_id,
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
        "by_provider": {},
        # by_provider 需要按结算时间排序算连败，先攒 (provider, payout, stake, settled_at)
        "_provider_seq": [],
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

        prov = str(bet.provider or "").strip() or "unknown"
        sel_l = str(bet.selection or "").strip().lower() or "unknown"
        pb = _bucket(stats["by_provider"], prov)
        pb["settled"] += 1
        pb.setdefault("stake", 0.0)
        pb.setdefault("payout", 0.0)
        pb["stake"] += stake
        pb["payout"] += payout
        if payout > stake + 1e-9:
            pb["won"] += 1
        elif payout < stake - 1e-9:
            pb["lost"] += 1
        # 站点×小球方向细分。
        psb = _bucket(pb.setdefault("by_selection", {}), sel_l)
        psb["settled"] += 1
        psb.setdefault("stake", 0.0)
        psb.setdefault("payout", 0.0)
        psb["stake"] += stake
        psb["payout"] += payout
        if payout > stake + 1e-9:
            psb["won"] += 1
        elif payout < stake - 1e-9:
            psb["lost"] += 1
        stats["_provider_seq"].append((prov, sel_l, payout, stake, bet.settled_at))

    decided = stats["won"] + stats["lost"]
    if decided:
        stats["win_rate"] = round(stats["won"] / decided, 4)
    if stats["stake"] > 0:
        stats["roi"] = round((stats["payout"] - stats["stake"]) / stats["stake"], 4)
    for store in (stats["by_sport"], stats["by_selection"]):
        for v in store.values():
            d = v["won"] + v["lost"]
            v["win_rate"] = round(v["won"] / d, 4) if d else None

    # by_provider：ROI + 当前连败（按结算时间倒序，从最新往回数连续全输单）
    # 含站点×方向细分（by_selection），供仓位策略做方向级分配
    prov_seq = {}
    prov_sel_seq = {}
    for prov, sel_l, payout, stake, s_at in stats.pop("_provider_seq", []):
        prov_seq.setdefault(prov, []).append((s_at, payout, stake))
        prov_sel_seq.setdefault((prov, sel_l), []).append((s_at, payout, stake))

    def _finalize_bucket(b: dict, seq: list) -> None:
        d = b["won"] + b["lost"]
        b["win_rate"] = round(b["won"] / d, 4) if d else None
        b["roi"] = (
            round((b["payout"] - b["stake"]) / b["stake"], 4)
            if b["stake"] > 0
            else None
        )
        streak = 0
        for _s_at, payout, stake in sorted(seq, key=lambda x: x[0] or datetime.min, reverse=True):
            if payout < stake - 1e-9:
                streak += 1
            else:
                break
        b["loss_streak"] = streak

    for prov, seq in prov_seq.items():
        _finalize_bucket(stats["by_provider"][prov], seq)
    for (prov, sel_l), seq in prov_sel_seq.items():
        pb = stats["by_provider"].get(prov)
        if pb and sel_l in (pb.get("by_selection") or {}):
            _finalize_bucket(pb["by_selection"][sel_l], seq)

    try:
        await cache.set_json(cache_key, stats, ttl=_SETTLED_CACHE_TTL)
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
