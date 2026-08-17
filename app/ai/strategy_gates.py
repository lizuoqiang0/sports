"""人工 / 自动共用的 AI 策略门禁：严格按配置参数运行。"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.strategy import (
    StrategyConfig,
    decision_passes_strategy,
    load_fresh_strategy,
)
from app.config import settings
from app.models.user import Bet


def min_stake_floor(strat: StrategyConfig | None = None) -> Decimal:
    """
    AI 路径最低仓位：严格服从策略「单笔最大金额」。
    不再用系统 MIN_BET_AMOUNT 抬高下限（避免配置 10 却被要求 ≥100）。
    """
    if strat is not None:
        hi = Decimal(str(float(getattr(strat, "max_bet_amount", 0) or 0)))
        if hi > 0:
            # 允许下到 1，上限由 max_bet_amount 约束
            return Decimal("1")
    return Decimal(str(float(getattr(settings, "MIN_BET_AMOUNT", 1) or 1)))


def stake_bounds(strat: StrategyConfig) -> tuple[Decimal, Decimal]:
    """返回 (最低, 最高)，最高=配置单笔最大金额。"""
    hi = Decimal(str(float(getattr(strat, "max_bet_amount", 0) or 0)))
    if hi <= 0:
        hi = Decimal(str(float(getattr(settings, "MIN_BET_AMOUNT", 1) or 1)))
    lo = Decimal("1")
    if hi < lo:
        hi = lo
    return lo, hi


def cap_stake(stake: Decimal | float, strat: StrategyConfig) -> Decimal:
    """仓位夹在 [1, 配置单笔最大金额]。0/负值回退最小注（不回退满仓）。"""
    s = Decimal(str(stake or 0))
    lo, hi = stake_bounds(strat)
    if s <= 0:
        return lo
    return min(max(s, lo), hi)


def team_is_excluded(home: str, away: str, excluded: list[str] | None) -> bool:
    if not excluded:
        return False
    home_l = (home or "").strip().lower()
    away_l = (away or "").strip().lower()
    for t in excluded:
        t = str(t or "").strip().lower()
        if not t:
            continue
        if t in home_l or t in away_l or home_l in t or away_l in t:
            return True
    return False


def sport_is_preferred(sport: str, preferred: list[str] | None) -> bool:
    """preferred 为空 = 不限制；否则必须命中。"""
    prefs = [str(x).lower().strip() for x in (preferred or []) if str(x).strip()]
    if not prefs:
        return True
    s = str(sport or "").lower().strip()
    if s == "soccer":
        s = "football"
    prefs = ["football" if p == "soccer" else p for p in prefs]
    return s in prefs


async def calc_daily_pnl(db: AsyncSession, user_id: int) -> Decimal:
    """日风控盈亏：以午夜总资产为基线，供止损/止盈门禁使用。"""
    from app.services.balances import load_site_balances
    from app.services.daily_pnl import get_daily_pnl

    site_balances = await load_site_balances(db, user_id)
    total_assets = sum(float(s.get("balance") or 0) for s in site_balances)
    pnl_info = await get_daily_pnl(user_id, total_assets)
    return Decimal(str(pnl_info["daily_pnl"]))


async def count_today_bets(db: AsyncSession, user_id: int) -> int:
    """今日 AI 相关注单笔数（与自动引擎一致）。"""
    today = datetime.now(timezone.utc).date()
    start = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)
    res = await db.execute(
        select(func.count(Bet.id)).where(
            Bet.user_id == int(user_id),
            Bet.is_ai_bet.is_(True),
            Bet.created_at >= start,
        )
    )
    return int(res.scalar() or 0)


async def check_daily_risk(
    db: AsyncSession,
    user_id: int,
    strat: StrategyConfig,
) -> tuple[bool, str]:
    """
    日止损 / 日止盈 / 每日笔数。
    返回 (triggered, reason)；triggered=True 表示应停止投注。
    """
    pnl = await calc_daily_pnl(db, user_id)
    stop = Decimal(str(strat.stop_loss or 0))
    take = Decimal(str(strat.take_profit or 0))
    if stop > 0 and pnl <= -stop:
        return True, f"触发止损线: 日亏损 {pnl} >= {stop}"
    if take > 0 and pnl >= take:
        return True, f"触发止盈线: 日收益 {pnl} >= {take}"
    n = await count_today_bets(db, user_id)
    max_n = int(strat.max_daily_bets or 10)
    if n >= max_n:
        return True, f"已达每日投注上限: {n}/{max_n}"
    return False, ""


async def gate_recommendation_for_place(
    *,
    user_id: int,
    rec: dict,
    stake: Decimal,
    db: AsyncSession,
) -> tuple[bool, str, Decimal, StrategyConfig]:
    """
    一键/手动下单前：完整策略校验。
    返回 (ok, reason, capped_stake, strat)。
    """
    _, strat = await load_fresh_strategy(user_id)
    r = rec.get("recommendation") or {}
    if not r.get("should_bet"):
        why = str(r.get("reasoning") or "策略未通过，不可下单")
        return False, why, Decimal("0"), strat
    triggered, risk_why = await check_daily_risk(db, user_id, strat)
    if triggered:
        return False, risk_why, Decimal("0"), strat
    capped = cap_stake(stake, strat)
    return True, "", capped, strat
