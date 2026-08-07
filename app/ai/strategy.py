"""
AI 投注策略引擎

核心策略:
1. 置信度/赔率/风控门禁
2. 固定仓位（策略单笔最大金额）
3. 动态止损止盈
"""
import logging
import math
from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel
from app.config import settings
from app.ai.analyzer import analyzer

logger = logging.getLogger(__name__)


# === 策略配置 ===
class StrategyConfig(BaseModel):
    """投注策略参数（预设 + 用户 AIConfig 覆盖后的生效值）"""
    name: str = "balanced"

    # 风控
    max_bet_percentage: float = settings.AI_MAX_BET_PERCENTAGE
    max_daily_loss_percentage: float = settings.AI_DAILY_LOSS_LIMIT
    max_concurrent_bets: int = settings.AI_MAX_CONCURRENT_BETS
    min_confidence: float = settings.AI_MIN_CONFIDENCE
    max_odds: float = settings.AI_MAX_ODDS
    min_odds: float = settings.AI_MIN_ODDS

    # 用户 AI 策略配置（绝对金额/次数）
    max_bet_amount: float = settings.AI_STRATEGY_MAX_BET_AMOUNT
    max_daily_bets: int = settings.AI_STRATEGY_MAX_DAILY_BETS
    stop_loss: float = settings.AI_STOP_LOSS
    take_profit: float = settings.AI_TAKE_PROFIT
    use_llm_analysis: bool = True

    # 策略参数
    kelly_fraction_cap: float = settings.AI_KELLY_FRACTION_CAP


# 预设策略
STRATEGIES = {
    "conservative": StrategyConfig(
        name="conservative",
        max_bet_percentage=settings.AI_CONS_MAX_BET_PCT,
        max_daily_loss_percentage=settings.AI_CONS_DAILY_LOSS,
        max_concurrent_bets=settings.AI_CONS_MAX_CONCURRENT,
        min_confidence=settings.AI_CONS_MIN_CONFIDENCE,
        kelly_fraction_cap=settings.AI_CONS_KELLY_CAP,
    ),
    "balanced": StrategyConfig(
        name="balanced",
        max_bet_percentage=settings.AI_MAX_BET_PERCENTAGE,
        max_daily_loss_percentage=settings.AI_DAILY_LOSS_LIMIT,
        max_concurrent_bets=settings.AI_MAX_CONCURRENT_BETS,
        min_confidence=settings.AI_MIN_CONFIDENCE,
        kelly_fraction_cap=settings.AI_KELLY_FRACTION_CAP,
    ),
    "aggressive": StrategyConfig(
        name="aggressive",
        max_bet_percentage=settings.AI_AGG_MAX_BET_PCT,
        max_daily_loss_percentage=settings.AI_AGG_DAILY_LOSS,
        max_concurrent_bets=settings.AI_AGG_MAX_CONCURRENT,
        min_confidence=settings.AI_AGG_MIN_CONFIDENCE,
        kelly_fraction_cap=settings.AI_AGG_KELLY_CAP,
    ),
}

# min_confidence 完全由用户 AI 策略配置决定，默认值取 settings.AI_MIN_CONFIDENCE


def effective_strategy_from_ai_config(ai_config) -> StrategyConfig:
    """以策略预设为底，严格覆盖用户在「AI 策略配置」里保存的数值。"""
    if ai_config is None:
        return STRATEGIES["balanced"].model_copy(deep=True)

    name = str(getattr(ai_config, "strategy", None) or "balanced").strip().lower()
    base = STRATEGIES.get(name, STRATEGIES["balanced"]).model_copy(deep=True)

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

    min_odds = max(1.01, _f("min_odds", base.min_odds))
    max_odds = max(min_odds, _f("max_odds", base.max_odds))
    min_conf = min(0.99, max(0.0, _f("min_confidence", base.min_confidence)))
    # 单笔上限严格取用户配置，不因系统 MIN_BET 抬高
    raw_amt = _f("max_bet_amount", base.max_bet_amount)
    max_amt = max(1.0, raw_amt) if raw_amt > 0 else 1.0

    return base.model_copy(
        update={
            "name": name or base.name,
            "min_confidence": min_conf,
            "min_odds": min_odds,
            "max_odds": max_odds,
            "max_bet_amount": max_amt,
            "max_daily_bets": max(1, _i("max_daily_bets", base.max_daily_bets)),
            "stop_loss": max(0.0, _f("stop_loss", base.stop_loss)),
            "take_profit": max(0.0, _f("take_profit", base.take_profit)),
            "use_llm_analysis": bool(getattr(ai_config, "use_llm_analysis", True)),
        }
    )


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
            return None, STRATEGIES["balanced"].model_copy(deep=True)
        # 抽出纯值快照，避免 DetachedInstanceError / 误持久化
        from types import SimpleNamespace

        # min_confidence 完全由用户 AI 策略配置决定，不强制地板

        snap = SimpleNamespace(
            user_id=row.user_id,
            strategy=row.strategy,
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
    conf = float(getattr(decision, "confidence", 0) or 0)
    if conf + 1e-9 < float(strat.min_confidence):
        return False, f"置信度 {conf:.2f} < 最新策略 {strat.min_confidence:.2f}"
    odds = float(getattr(decision, "odds", 0) or 0)
    if odds + 1e-9 < float(strat.min_odds):
        return False, f"赔率 {odds} < 最新策略下限 {strat.min_odds}"
    if odds - 1e-9 > float(strat.max_odds):
        return False, f"赔率 {odds} > 最新策略上限 {strat.max_odds}"
    stake = float(getattr(decision, "suggested_stake", 0) or 0)
    if stake > float(strat.max_bet_amount) + 1e-6:
        return False, f"仓位 {stake} > 配置单笔上限 {strat.max_bet_amount}"
    # 严格按策略：只要 >0 且不超过单笔上限即可（不再用系统 MIN_BET 抬高）
    if stake + 1e-9 < 1.0:
        return False, "仓位需 ≥1"
    return True, ""


# === 投注决策 ===
_VALID_SELECTIONS = {
    "total": {"over", "under"},
    "moneyline": {"home", "away", "draw"},
    "spread": {"home", "away"},
}


class BetDecision(BaseModel):
    """AI投注决策（足球：胜负/让球/大小；篮球：大小）"""
    match_id: int
    selection: str  # over/under/home/away/draw
    confidence: float
    expected_value: float           # 仅展示，不参与决策
    kelly_fraction: float           # 仅展示，不参与决策
    suggested_stake: Decimal
    reasoning: str
    risk_score: float               # 仅展示，不参与决策
    should_bet: bool   # 最终决策
    bet_type: str = "total"
    provider_code: str = ""
    odds: float = 0.0
    line: Optional[float] = None


class StrategyEngine:
    """
    策略引擎 - 综合评估是否下注及下注金额
    """

    def __init__(self, config: Optional[StrategyConfig] = None):
        self.config = config or STRATEGIES["balanced"]

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
        综合评估一场赛事，决定是否投注

        决策流程:
        1. 置信度过滤
        2. 赔率范围检查
        3. 风控检查 (日亏损/持仓数)
        4. 期望值计算
        5. 凯利仓位计算
        6. 分散化调整
        """
        # 1. 基础过滤
        if analysis.get("consensus_reached") is False:
            return self._reject(match_info, analysis, "模型共识未达成")

        confidence = analysis.get("confidence", 0)
        if confidence < self.config.min_confidence:
            return self._reject(match_info, analysis, f"置信度不足: {confidence:.2f} < {self.config.min_confidence}")

        prediction = str(analysis.get("prediction", "") or "").lower()
        bet_type = str(analysis.get("bet_type") or match_info.get("bet_type") or "total").lower()
        if bet_type not in _VALID_SELECTIONS:
            bet_type = "total"
        allowed_sels = _VALID_SELECTIONS[bet_type]
        if prediction not in allowed_sels:
            return self._reject(
                match_info,
                analysis,
                f"不支持的投注方向: {bet_type}/{prediction}",
            )

        # 2. 赔率检查
        odds_data = match_info.get("odds", {}) or {}
        try:
            odds = float(odds_data.get(prediction) or analysis.get("odds") or 0)
        except (TypeError, ValueError):
            odds = 0.0
        if odds < self.config.min_odds:
            return self._reject(match_info, analysis, f"赔率过低: {odds} < {self.config.min_odds}")
        if odds > self.config.max_odds:
            return self._reject(match_info, analysis, f"赔率过高(风险大): {odds} > {self.config.max_odds}")

        # 2b. EV 硬门禁：期望价值必须为正且超过最低阈值
        ev = float(analysis.get("expected_value", 0) or 0)
        if ev < 0.02:
            return self._reject(match_info, analysis, f"期望价值不足: EV={ev:.4f} < 0.02")

        # 3. 风控检查
        if active_bets_count >= self.config.max_concurrent_bets:
            return self._reject(match_info, analysis, f"持仓已满: {active_bets_count}/{self.config.max_concurrent_bets}")

        # 优先用绝对止损线（AI 策略配置）；否则回退百分比
        abs_stop = Decimal(str(self.config.stop_loss or 0))
        if abs_stop > 0 and daily_loss >= abs_stop:
            return self._reject(match_info, analysis, f"触发日止损: {daily_loss} >= {abs_stop}")
        if abs_stop <= 0:
            max_daily_loss = user_balance * Decimal(str(self.config.max_daily_loss_percentage))
            if daily_loss >= max_daily_loss:
                return self._reject(match_info, analysis, f"日亏损已达上限: {daily_loss}/{max_daily_loss}")

        # 4. 凯利仓位：高 EV 多下、低 EV 少下（Kelly 分数 × 倍数，限制在 [min_stake, max_amt]）
        kelly = analysis.get("kelly_fraction", 0)
        kelly = min(kelly, self.config.kelly_fraction_cap)

        # 6. 投注金额：Kelly 动态仓位（最低 10%，最高 100% 单笔上限）
        max_amt = Decimal(str(self.config.max_bet_amount or 1))
        if max_amt < 1:
            return self._reject(match_info, analysis, f"单笔上限无效: {max_amt}")
        # Kelly 分数映射到仓位比例：kelly=0 -> 10%, kelly=0.25 -> 100%
        kelly_ratio = max(0.10, min(1.0, float(kelly) * 4.0))
        suggested_stake = (max_amt * Decimal(str(kelly_ratio))).quantize(Decimal("0.01"))
        min_stake = (max_amt * Decimal("0.10")).quantize(Decimal("0.01"))
        if suggested_stake < min_stake:
            suggested_stake = min_stake
        if user_balance > 0 and user_balance < min_stake:
            return self._reject(
                match_info, analysis, f"余额不足最低仓位: {user_balance} < {min_stake}"
            )

        # 风险评分（ev 已在 2b 步骤计算）
        risk_score = self._calc_risk_score(confidence, odds, ev, active_bets_count)

        line = analysis.get("line")
        try:
            line_f = float(line) if line is not None else None
        except (TypeError, ValueError):
            line_f = None

        return BetDecision(
            match_id=match_info.get("id"),
            selection=prediction,
            confidence=confidence,
            expected_value=ev,
            kelly_fraction=kelly,
            suggested_stake=suggested_stake.quantize(Decimal("0.01")),
            reasoning=analysis.get("reasoning", ""),
            risk_score=risk_score,
            should_bet=True,
            bet_type=bet_type,
            provider_code=str(analysis.get("provider_code") or match_info.get("provider_code") or ""),
            odds=float(odds),
            line=line_f,
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
        使用多目标优化: 期望收益 vs 风险分散
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

        # 按期望价值排序
        # 利益最大化：期望利润（仓位×EV）优先，其次 EV / 胜率 / 赔率
        def _rank(d: BetDecision) -> tuple:
            conf = float(d.confidence or 0)
            if conf > 1:
                conf /= 100.0
            od = float(d.odds or 0)
            edge = conf * od - 1.0 if od > 1 else float(d.expected_value or 0)
            stake = float(d.suggested_stake or 0)
            return (edge * stake if stake > 0 else edge, edge, conf, od)

        decisions.sort(key=_rank, reverse=True)

        # 多样性过滤 (避免同一联赛过多)
        diversified = self._diversify(decisions, max_bets)

        return diversified[:max_bets]

    # === 止损止盈 ===
    def should_stop_trading(
        self,
        daily_pnl: Decimal,
        balance: Decimal,
        stop_loss_pct: float = settings.AI_DAILY_LOSS_LIMIT,
        take_profit_pct: float = settings.AI_TAKE_PROFIT_PCT,
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
        ev = float(analysis.get("expected_value", 0) or 0)
        gate = reason.split(":")[0].strip() if ":" in reason else reason[:20]
        logger.info(
            "❌ 策略拒绝 | match=%s %s vs %s | sel=%s conf=%.2f odds=%.2f EV=%.4f | 门禁=%s | %s",
            mid, ht, at, sel, conf, odds, ev, gate, reason,
        )
        return BetDecision(
            match_id=match_info.get("id"),
            selection=analysis.get("prediction", "under"),
            confidence=analysis.get("confidence", 0),
            expected_value=analysis.get("expected_value", 0),
            kelly_fraction=0,
            suggested_stake=Decimal("0"),
            reasoning=f"[不投注] {reason}",
            risk_score=1.0,
            should_bet=False,
        )

    def _calc_risk_score(self, confidence: float, odds: float, ev: float, active_count: int) -> float:
        """综合风险评分"""
        # 低置信度 -> 高风险
        risk = (1 - confidence) * settings.AI_RISK_LOW_CONF_WEIGHT

        # 高赔率 -> 高风险
        if odds > settings.AI_RISK_HIGH_ODDS_THRESHOLD:
            risk += settings.AI_RISK_HIGH_ODDS_PENALTY
        elif odds > settings.AI_RISK_MID_ODDS_THRESHOLD:
            risk += settings.AI_RISK_MID_ODDS_PENALTY

        # 低EV -> 高风险
        if ev < settings.AI_RISK_LOW_EV_THRESHOLD:
            risk += settings.AI_RISK_LOW_EV_PENALTY

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
strategy_engine = StrategyEngine()
