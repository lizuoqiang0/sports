"""人工 / 自动共用的 AI 策略门禁：严格按配置参数运行。"""
from __future__ import annotations

import logging
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
from app.models.user import Bet, BetStatus

logger = logging.getLogger(__name__)


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


def resolve_site_minimum_stake(
    *,
    requested_stake: Decimal | float,
    dynamic_stake: Decimal | float,
    site_minimum: Decimal | float,
    max_stake: Decimal | float,
    available_balance: Decimal | float | None = None,
) -> tuple[Decimal | None, str]:
    """按站点最低额调整仓位，并保留策略上限和余额两道硬门禁。"""
    requested = Decimal(str(requested_stake or 0))
    dynamic = Decimal(str(dynamic_stake or 0))
    minimum = Decimal(str(site_minimum or 0))
    maximum = Decimal(str(max_stake or 0))
    if requested <= 0 or maximum <= 0:
        return None, "invalid_stake_policy"
    if requested > maximum:
        return None, "requested_stake_exceeds_strategy_cap"

    # 仅在站点最低额高于本次请求时，才提升到策略给出的动态仓位；
    # 动态仓位仍不足时才以站点最低额为准。
    adjusted = minimum > requested
    target = max(requested, dynamic, minimum) if adjusted else requested
    if target > maximum:
        return None, "site_minimum_exceeds_strategy_cap"
    if available_balance is not None:
        balance = Decimal(str(available_balance or 0))
        if balance <= 0 or target > balance:
            return None, "site_minimum_exceeds_available_balance"
    reason = "site_minimum_adjusted" if adjusted else "requested_stake"
    return target.quantize(Decimal("0.01")), reason


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
    """日风控盈亏：当日总资产变化 + 未结算注单 stake（避免 pending 被误计为亏损）。

    数据可信性保护：所有站点余额都非 live（src=none，如 Gate 会话掉线）
    时，总资产读数不可信（读 0 会让基线差被误判成巨额亏损触发止损）。
    此时返回 Decimal(0)（中性值，不触发风控），等余额恢复再正常计算。
    """
    from app.services.balances import load_site_balances
    from app.services.daily_pnl import get_daily_pnl

    site_balances = await load_site_balances(db, user_id)
    live_sites = [s for s in site_balances if s.get("is_live") or s.get("live")]
    if site_balances and not live_sites:
        logger.warning(
            "calc_daily_pnl: 所有站点余额均非实时（Gate 会话掉线？），跳过风控盈亏计算 user=%s",
            user_id,
        )
        return Decimal("0")
    total_assets = sum(float(s.get("balance") or 0) for s in site_balances)

    # 加回未结算注单的 stake（站点余额已扣除 pending stake，但未结算≠亏损）
    pending_result = await db.execute(
        select(func.sum(Bet.stake)).where(
            Bet.user_id == int(user_id),
            Bet.status == BetStatus.SUCCESS,
            Bet.actual_payout.is_(None),
        )
    )
    pending_stake = float(pending_result.scalar() or 0)
    adjusted_total = total_assets + pending_stake

    pnl_info = await get_daily_pnl(user_id, adjusted_total)
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
        return True, f"触发止损线: 日亏损 {abs(float(pnl))} >= 止损额 {stop}"
    if take > 0 and pnl >= take:
        return True, f"触发止盈线: 日收益 {float(pnl)} >= 止盈额 {take}"
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
    ai_config, strat = await load_fresh_strategy(user_id)
    r = rec.get("recommendation") or {}
    if not r.get("should_bet"):
        why = str(r.get("reasoning") or "策略未通过，不可下单")
        return False, why, Decimal("0"), strat

    # 球队排除检查
    home = str(rec.get("home_team") or rec.get("home") or "")
    away = str(rec.get("away_team") or rec.get("away") or "")
    excluded = list(ai_config.excluded_teams) if ai_config and ai_config.excluded_teams else None
    if team_is_excluded(home, away, excluded):
        return False, "球队在排除名单中", Decimal("0"), strat

    # 球类偏好检查
    sport = str(rec.get("sport") or "")
    preferred = list(ai_config.preferred_sports) if ai_config and ai_config.preferred_sports else None
    if not sport_is_preferred(sport, preferred):
        return False, f"球类 {sport} 不在偏好列表中", Decimal("0"), strat

    triggered, risk_why = await check_daily_risk(db, user_id, strat)
    if triggered:
        return False, risk_why, Decimal("0"), strat
    capped = cap_stake(stake, strat)
    return True, "", capped, strat
