"""
AI 投注策略引擎

简化模式：仅按 AI 分析给出的大小球百分比进行下注
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Optional

from pydantic import BaseModel
from app.config import settings

logger = logging.getLogger(__name__)


# === 策略配置 ===
class StrategyConfig(BaseModel):
    """投注策略参数（预设 + 用户 AIConfig 覆盖后的生效值）"""
    name: str = "simple"

    # 用户 AI 策略配置（绝对金额/次数）
    max_bet_amount: float = settings.AI_STRATEGY_MAX_BET_AMOUNT
    max_daily_bets: int = settings.AI_STRATEGY_MAX_DAILY_BETS
    stop_loss: float = settings.AI_STOP_LOSS
    take_profit: float = settings.AI_TAKE_PROFIT
    use_llm_analysis: bool = True

    min_confidence: float = 0.0    # 最低AI置信度（0=不限制）
    min_odds: float = 1.1          # 最低赔率
    max_odds: float = 10.0         # 最高赔率


# 唯一策略：仅保留 simple 模式
STRATEGIES = {
    "simple": StrategyConfig(name="simple"),
}

STRATEGY_DESCRIPTIONS = {
    "simple": "简化策略 - 仅按 AI 分析给出的大小球百分比进行下注",
}

AI_ONE_CLICK_MIN_STAKE = 1.0


def effective_strategy_from_ai_config(ai_config) -> StrategyConfig:
    """以 simple 预设为底，覆盖用户在配置里保存的数值。"""
    if ai_config is None:
        return STRATEGIES["simple"].model_copy(deep=True)

    name = "simple"
    base = STRATEGIES["simple"].model_copy(deep=True)

    def _f(attr: str, default: float) -> float:
        try:
            v = getattr(ai_config, attr, None)
            return float(v) if v is not None else default
        except (TypeError, ValueError):
            return default

    def _i(attr: str, default: int) -> int:
        try:
            v = getattr(ai_config, attr, None)
            return int(v) if v is not None else default
        except (TypeError, ValueError):
            return default

    # 单笔上限严格取用户配置，不因系统 MIN_BET 抬高
    raw_amt = _f("max_bet_amount", base.max_bet_amount)
    max_amt = max(1.0, raw_amt) if raw_amt > 0 else 1.0
    max_daily_bets = max(1, _i("max_daily_bets", base.max_daily_bets))

    return base.model_copy(
        update={
            "name": base.name,
            "max_bet_amount": max_amt,
            "max_daily_bets": max_daily_bets,
            "stop_loss": max(0.0, _f("stop_loss", base.stop_loss)),
            "take_profit": max(0.0, _f("take_profit", base.take_profit)),
            "use_llm_analysis": bool(getattr(ai_config, "use_llm_analysis", True)),
            "min_confidence": max(0.0, _f("min_confidence", base.min_confidence)),
            "min_odds": max(1.01, _f("min_odds", base.min_odds)),
            "max_odds": max(1.01, _f("max_odds", base.max_odds)),
        }
    )


def strategy_public_payload(strat: StrategyConfig) -> dict[str, Any]:
    return {
        "name": str(strat.name),
        "description": STRATEGY_DESCRIPTIONS.get(str(strat.name), ""),
        "max_bet_amount": float(strat.max_bet_amount),
        "max_daily_bets": int(strat.max_daily_bets),
        "stop_loss": float(strat.stop_loss),
        "take_profit": float(strat.take_profit),
        "min_confidence": float(strat.min_confidence),
        "min_odds": float(strat.min_odds),
        "max_odds": float(strat.max_odds),
    }


def _list_attr(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(x).strip() for x in value if str(x).strip()]
    return []


def ai_config_response_payload(ai_config: Any | None) -> dict[str, Any]:
    """统一 AI 配置对外返回结构，避免前后端各自拼默认值。"""
    effective = (
        effective_strategy_from_ai_config(ai_config)
        if ai_config is not None
        else STRATEGIES["simple"].model_copy(deep=True)
    )
    scan_interval_sec = max(30, int(getattr(settings, "AI_SCAN_INTERVAL_SEC", 30) or 30))
    max_bets_per_cycle = max(1, int(getattr(settings, "AI_MAX_BETS_PER_CYCLE", 3) or 3))
    return {
        "is_active": bool(getattr(ai_config, "is_active", False)) if ai_config is not None else False,
        "strategy": effective.name,
        "max_bet_amount": float(effective.max_bet_amount),
        "max_daily_bets": int(effective.max_daily_bets),
        "preferred_sports": _list_attr(getattr(ai_config, "preferred_sports", [])),
        "excluded_teams": _list_attr(getattr(ai_config, "excluded_teams", [])),
        "stop_loss": float(getattr(ai_config, "stop_loss", effective.stop_loss)),
        "take_profit": float(getattr(ai_config, "take_profit", effective.take_profit)),
        "use_llm_analysis": bool(effective.use_llm_analysis),
        "min_confidence": float(getattr(ai_config, "min_confidence", 0.0)) if ai_config is not None else 0.0,
        "min_odds": float(getattr(ai_config, "min_odds", 1.1)) if ai_config is not None else 1.1,
        "max_odds": float(getattr(ai_config, "max_odds", 10.0)) if ai_config is not None else 10.0,
        "auto_cashout": bool(getattr(ai_config, "auto_cashout", False)),
        "cashout_threshold": float(
            getattr(ai_config, "cashout_threshold", settings.AI_DEFAULT_CASHOUT_THRESHOLD)
        ),
        "min_bet_amount": AI_ONE_CLICK_MIN_STAKE,
        "one_click_min_stake": AI_ONE_CLICK_MIN_STAKE,
        "environment": settings.ENVIRONMENT,
        "strategy_profile": strategy_public_payload(effective),
        "runtime_limits": {
            "scan_interval_sec": scan_interval_sec,
            "scan_interval_min": round(scan_interval_sec / 60, 2),
            "max_bets_per_cycle": max_bets_per_cycle,
            "distinct_matches_per_cycle": True,
        },
    }


def _as_float(value: Any, default: Any = 0.0) -> Any:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


async def load_fresh_strategy(user_id: int):
    """
    每次从 DB 读取最新 AIConfig（绕过会话缓存），配置保存后立即生效。
    Returns: (AIConfig|None, StrategyConfig)
    """
    from sqlalchemy import select

    from app.database import AsyncSessionLocal
    from app.models.user import AIConfig

    async with AsyncSessionLocal() as db:
        # populate_existing：多 worker / 长周期内也拿到刚写入的值
        result = await db.execute(
            select(AIConfig)
            .where(AIConfig.user_id == int(user_id))
            .execution_options(populate_existing=True)
        )
        row = result.scalar_one_or_none()
        if not row:
            await db.commit()
            return None, STRATEGIES["simple"].model_copy(deep=True)
        # 抽出纯值快照，避免 DetachedInstanceError / 误持久化
        from types import SimpleNamespace

        snap = SimpleNamespace(
            user_id=row.user_id,
            strategy="simple",
            max_bet_amount=row.max_bet_amount,
            max_daily_bets=row.max_daily_bets,
            min_confidence=row.min_confidence,
            preferred_sports=list(row.preferred_sports or []),
            excluded_teams=list(row.excluded_teams or []),
            stop_loss=row.stop_loss,
            take_profit=row.take_profit,
            max_odds=row.max_odds,
            min_odds=row.min_odds,
            use_llm_analysis=bool(row.use_llm_analysis),
            auto_cashout=bool(row.auto_cashout),
            cashout_threshold=row.cashout_threshold,
            is_active=bool(row.is_active),
        )
        cfg = effective_strategy_from_ai_config(snap)
        await db.commit()
        return snap, cfg


def decision_passes_strategy(decision, strat: StrategyConfig) -> tuple[bool, str]:
    """用最新策略参数快速校验已有决策（配置热更新后下单前再拦一道）。"""
    if not decision or not getattr(decision, "should_bet", False):
        return False, "未通过投注决策"
    odds = float(getattr(decision, "odds", 0) or 0)
    if odds <= 1.0:
        return False, f"赔率无效: {odds}"
    stake = float(getattr(decision, "suggested_stake", 0) or 0)
    if stake + 1e-9 < 1.0:
        return False, "仓位需 ≥1"
    return True, ""


# === 投注决策 ===
_VALID_SELECTIONS = {
    "total": {"over", "under"},
}


class BetDecision(BaseModel):
    """AI投注决策（仅大小球）"""
    match_id: int
    selection: str  # over/under
    confidence: float
    suggested_stake: Decimal
    reasoning: str
    risk_score: float               # 仅展示，不参与决策
    should_bet: bool   # 最终决策
    bet_type: str = "total"
    provider_code: str = ""
    odds: float = 0.0
    line: Optional[float] = None
    sport: str = ""
    period: str = ""
    clock: str = ""
    home_score: int = 0
    away_score: int = 0


class StrategyEngine:
    """
    策略引擎 - 仅按 AI 分析给出的大小球百分比进行下注
    """

    def __init__(self, config: Optional[StrategyConfig] = None):
        self.config = config or STRATEGIES["simple"]

    # === 核心决策方法 ===
    async def evaluate_bet(
        self,
        match_info: dict,
        analysis: dict,
        user_balance: Decimal,
        daily_loss: Decimal,
        active_bets_count: int,
    ) -> BetDecision:
        """
        综合评估一场赛事，决定是否投注。

        当前模式仅保留:
        - AI 分析方向（大小球百分比）
        - 基础方向检查 + 赔率有效性检查
        """
        confidence = analysis.get("confidence", 0)
        prediction = str(analysis.get("prediction", "") or "").lower()
        bet_type = "total"
        mid = match_info.get("id", "?")
        ht = match_info.get("home_team", "?")
        at = match_info.get("away_team", "?")

        logger.info(
            "[策略评估] match=%s %s vs %s | pred=%s conf=%.2f consensus=%s | 余额=%.2f 日亏=%.2f 活跃=%d",
            mid, ht, at, prediction, float(confidence or 0),
            analysis.get("consensus_reached"),
            float(user_balance), float(daily_loss), active_bets_count,
        )

        if prediction not in ("over", "under"):
            logger.info("[策略评估] ❌ match=%s 投注方向无效: %s", mid, prediction)
            return self._reject(match_info, analysis, f"不支持的投注方向: {prediction}")

        # 赔率仅做有效性检查，不再做区间门槛
        odds_data = match_info.get("odds", {}) or {}
        try:
            odds = float(odds_data.get(prediction) or analysis.get("odds") or 0)
        except (TypeError, ValueError):
            odds = 0.0
        logger.info(
            "[策略评估] match=%s 赔率检查 | odds_data=%s | 取值=%.2f (from %s)",
            mid, odds_data, odds,
            "odds_data" if odds_data.get(prediction) else "analysis",
        )
        if odds < float(self.config.min_odds):
            logger.info("[策略评估] ❌ match=%s 赔率 %.2f < 最低 %.2f", mid, odds, float(self.config.min_odds))
            return self._reject(match_info, analysis, f"赔率 {odds} 低于最低 {self.config.min_odds}")
        if odds > float(self.config.max_odds):
            logger.info("[策略评估] ❌ match=%s 赔率 %.2f > 最高 %.2f", mid, odds, float(self.config.max_odds))
            return self._reject(match_info, analysis, f"赔率 {odds} 高于最高 {self.config.max_odds}")

        # 仅按 AI 分析结果决定
        if not analysis.get("consensus_reached", False):
            logger.info("[策略评估] ❌ match=%s AI 共识未达成", mid)
            return self._reject(match_info, analysis, "AI 共识未达成")

        # 最低置信度检查
        min_conf = float(self.config.min_confidence or 0.0)
        if float(confidence or 0) < min_conf:
            logger.info("[策略评估] ❌ match=%s 置信度 %.2f < 最低 %.2f", mid, float(confidence or 0), min_conf)
            return self._reject(match_info, analysis, f"置信度 {confidence:.2f} 低于最低 {min_conf:.2f}")

        # 投注金额：固定按策略单笔上限出手，不再做动态调仓
        max_amt = Decimal(str(self.config.max_bet_amount or 1))
        suggested_stake = max(max_amt, Decimal("1.00")).quantize(Decimal("0.01"))

        risk_score = self._calc_risk_score(confidence, odds, active_bets_count)

        logger.info(
            "[策略评估] ✅ match=%s 通过 | sel=%s conf=%.2f odds=%.2f | 下注=%.2f risk=%.2f max_bet=%.2f",
            mid, prediction, float(confidence or 0), odds,
            float(suggested_stake), risk_score, float(self.config.max_bet_amount or 0),
        )

        return BetDecision(
            match_id=match_info.get("id"),
            selection=prediction,
            confidence=confidence,
            suggested_stake=suggested_stake,
            reasoning=analysis.get("reasoning", ""),
            risk_score=risk_score,
            should_bet=True,
            bet_type="total",
            odds=float(odds),
            sport=str(match_info.get("sport") or ""),
            period=str(match_info.get("period") or ""),
            clock=str(match_info.get("clock") or ""),
            home_score=int(_as_float(match_info.get("home_score"), 0)),
            away_score=int(_as_float(match_info.get("away_score"), 0)),
            provider_code=str(analysis.get("provider_code") or match_info.get("provider_code") or ""),
        )

    # === 批量筛选 ===
    async def select_best_bets(
        self,
        candidates: list[dict],  # [{match_info, analysis}, ...]
        user_balance: Decimal,
        max_bets: int = settings.MAX_BETS_PER_MATCH,
    ) -> list[BetDecision]:
        """
        从多个候选中筛选最佳投注组合
        使用多目标优化: 置信度 / 赔率 / 风险分散
        """
        decisions = []

        for cand in candidates:
            # 简化的评估 (实际应传入完整上下文)
            decision = await self.evaluate_bet(
                match_info=cand["match_info"],
                analysis=cand["analysis"],
                user_balance=user_balance,
                daily_loss=Decimal("0"),
                active_bets_count=len(decisions),
            )
            if decision.should_bet:
                decisions.append(decision)

        # 按胜率与赔率排序
        def _rank(d: BetDecision) -> tuple:
            conf = float(d.confidence or 0)
            if conf > 1:
                conf /= 100.0
            od = float(d.odds or 0)
            stake = float(d.suggested_stake or 0)
            return (conf, od, stake)

        decisions.sort(key=_rank, reverse=True)

        # 多样性过滤 (避免同一联赛过多)
        diversified = self._diversify(decisions, max_bets)

        return diversified[:max_bets]

    # === 止损止盈 ===
    def should_stop_trading(
        self,
        daily_pnl: Decimal,
        balance: Decimal,
        stop_loss_pct: float = 0.20,
        take_profit_pct: float = 0.50,
    ) -> tuple[bool, str]:
        """
        判断是否应该暂停交易

        Returns:
            (should_stop, reason)
        """
        daily_return = daily_pnl / balance if balance > 0 else Decimal("0")

        if daily_return <= -Decimal(str(stop_loss_pct)):
            return True, f"触发止损: 日亏损 {daily_return*100:.1f}% >= {stop_loss_pct*100}%"

        if daily_return >= Decimal(str(take_profit_pct)):
            return True, f"触发止盈: 日收益 {daily_return*100:.1f}% >= {take_profit_pct*100}%"

        return False, ""

    # === 辅助方法 ===
    def _reject(self, match_info: dict, analysis: dict, reason: str) -> BetDecision:
        """创建拒绝决策，并记录结构化日志"""
        mid = match_info.get("id", "?")
        ht = match_info.get("home_team", "?")
        at = match_info.get("away_team", "?")
        sel = analysis.get("prediction", "under")
        conf = float(analysis.get("confidence", 0) or 0)
        odds = float(analysis.get("odds", 0) or 0)
        gate = reason.split(":")[0].strip() if ":" in reason else reason[:20]
        logger.info(
            "❌ 策略拒绝 | match=%s %s vs %s | sel=%s conf=%.2f odds=%.2f | 门禁=%s | %s",
            mid, ht, at, sel, conf, odds, gate, reason,
        )
        return BetDecision(
            match_id=match_info.get("id"),
            selection=analysis.get("prediction", "under"),
            confidence=analysis.get("confidence", 0),
            suggested_stake=Decimal("0"),
            reasoning=f"[不投注] {reason}",
            risk_score=1.0,
            should_bet=False,
            bet_type="total",
            provider_code=str(analysis.get("provider_code") or match_info.get("provider_code") or ""),
            odds=odds,
            line=_as_float(analysis.get("line"), None) if analysis.get("line") is not None else None,
            sport=str(match_info.get("sport") or ""),
            period=str(match_info.get("period") or ""),
            clock=str(match_info.get("clock") or ""),
            home_score=int(_as_float(match_info.get("home_score"), 0)),
            away_score=int(_as_float(match_info.get("away_score"), 0)),
        )

    def _calc_risk_score(self, confidence: float, odds: float, active_count: int) -> float:
        """综合风险评分"""
        # 低置信度 -> 高风险
        risk = (1 - confidence) * settings.AI_RISK_LOW_CONF_WEIGHT

        # 高赔率 -> 高风险
        if odds > settings.AI_RISK_HIGH_ODDS_THRESHOLD:
            risk += settings.AI_RISK_HIGH_ODDS_PENALTY
        elif odds > settings.AI_RISK_MID_ODDS_THRESHOLD:
            risk += settings.AI_RISK_MID_ODDS_PENALTY

        # 持仓多 -> 略增风险
        risk += min(active_count * settings.AI_RISK_ACTIVE_PENALTY, settings.AI_RISK_ACTIVE_CAP)

        return round(min(risk, 1.0), 2)

    def _diversify(self, decisions: list[BetDecision], max_bets: int) -> list[BetDecision]:
        """多样性过滤 - 避免同一联赛/类型过度集中"""
        selected = []
        league_count: dict[str, int] = {}

        for d in decisions:
            if len(selected) >= max_bets:
                break

            # 获取联赛信息 (需要从match_info获取，这里简化处理)
            # 实际实现应从决策中携带league信息
            league = "unknown"

            current_count = league_count.get(league, 0)
            if current_count >= settings.AI_DIVERSIFY_MAX_PER_LEAGUE:
                continue

            selected.append(d)
            league_count[league] = current_count + 1

        return selected


# 全局策略引擎
strategy_engine = StrategyEngine(STRATEGIES["simple"])
