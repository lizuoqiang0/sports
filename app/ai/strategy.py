"""生产 AI 全场大小球策略：唯一组合闸门实现。"""
from __future__ import annotations

import logging
from decimal import Decimal, ROUND_HALF_UP
from types import SimpleNamespace
from typing import Any, Optional

from pydantic import BaseModel

from app.ai.balanced_gate import evaluate_balanced_gate
from app.config import settings
from app.core.convert import to_float as _as_float

logger = logging.getLogger(__name__)
AI_MIN_STAKE = 1.0


class StrategyConfig(BaseModel):
    name: str = "balanced"
    max_bet_amount: float = settings.AI_STRATEGY_MAX_BET_AMOUNT
    max_daily_bets: int = settings.AI_STRATEGY_MAX_DAILY_BETS
    stop_loss: float = settings.AI_STOP_LOSS
    take_profit: float = settings.AI_TAKE_PROFIT
    use_llm_analysis: bool = True
    min_confidence: float = settings.AI_MIN_CONFIDENCE
    min_odds: float = settings.AI_MIN_ODDS
    max_odds: Optional[float] = settings.AI_MAX_ODDS


DEFAULT_STRATEGY = StrategyConfig()

LEAGUE_BLACKLIST_KEYWORDS: tuple[str, ...] = (
    "u16", "u17", "u18", "u19", "u20", "u21", "u23",
    "青年", "青少年", "后备队", "预备队", "女子", "女足", "女篮",
    "women", "友谊赛", "表演赛", "冰岛", "iceland",
    "新南威尔士", "nsw", "威尔士", "wales",
)


def league_is_blacklisted(league: str) -> bool:
    value = str(league or "").lower()
    return bool(value) and any(keyword in value for keyword in LEAGUE_BLACKLIST_KEYWORDS)


def _float_attr(obj: Any, name: str, default: float) -> float:
    try:
        value = getattr(obj, name, None)
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _int_attr(obj: Any, name: str, default: int) -> int:
    try:
        value = getattr(obj, name, None)
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def effective_strategy_from_ai_config(ai_config: Any | None) -> StrategyConfig:
    """用户设置可收紧概率/赔率/金额，不能替换生产组合闸门。"""
    base = DEFAULT_STRATEGY.model_copy(deep=True)
    if ai_config is None:
        return base
    amount = _float_attr(ai_config, "max_bet_amount", base.max_bet_amount)
    return base.model_copy(update={
        "max_bet_amount": max(1.0, amount) if amount > 0 else 1.0,
        "max_daily_bets": max(1, _int_attr(ai_config, "max_daily_bets", base.max_daily_bets)),
        "stop_loss": max(0.0, _float_attr(ai_config, "stop_loss", base.stop_loss)),
        "take_profit": max(0.0, _float_attr(ai_config, "take_profit", base.take_profit)),
        "use_llm_analysis": bool(getattr(ai_config, "use_llm_analysis", True)),
        "min_confidence": max(0.0, _float_attr(ai_config, "min_confidence", base.min_confidence)),
        "min_odds": max(1.01, _float_attr(ai_config, "min_odds", base.min_odds)),
        "max_odds": max(1.01, _float_attr(ai_config, "max_odds", float(base.max_odds or 99))),
    })


def _list_attr(value: Any) -> list[str]:
    return [str(item).strip() for item in value if str(item).strip()] if isinstance(value, (list, tuple)) else []


def ai_config_response_payload(ai_config: Any | None) -> dict[str, Any]:
    effective = effective_strategy_from_ai_config(ai_config)
    seconds = max(120, int(getattr(settings, "AI_SCAN_INTERVAL_SEC", 120) or 120))
    return {
        "is_active": bool(getattr(ai_config, "is_active", False)) if ai_config is not None else False,
        "strategy": effective.name,
        "max_bet_amount": float(effective.max_bet_amount),
        "max_daily_bets": int(effective.max_daily_bets),
        "preferred_sports": _list_attr(getattr(ai_config, "preferred_sports", [])),
        "excluded_teams": _list_attr(getattr(ai_config, "excluded_teams", [])),
        "stop_loss": float(effective.stop_loss),
        "take_profit": float(effective.take_profit),
        "use_llm_analysis": bool(effective.use_llm_analysis),
        "min_confidence": float(effective.min_confidence),
        "min_odds": float(effective.min_odds),
        "max_odds": float(effective.max_odds or 99),
        "min_bet_amount": AI_MIN_STAKE,
        "runtime_limits": {
            "scan_interval_sec": seconds,
            "scan_interval_min": round(seconds / 60, 2),
            "stream_bet_mode": True,
            "combined_gate": True,
        },
    }


async def load_fresh_strategy(user_id: int):
    from sqlalchemy import select
    from app.database import AsyncSessionLocal
    from app.models.user import AIConfig

    async with AsyncSessionLocal() as db:
        row = (await db.execute(
            select(AIConfig).where(AIConfig.user_id == int(user_id)).execution_options(populate_existing=True)
        )).scalar_one_or_none()
        if row is None:
            await db.commit()
            return None, DEFAULT_STRATEGY.model_copy(deep=True)
        snapshot = SimpleNamespace(
            user_id=row.user_id,
            strategy="balanced",
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
            is_active=bool(row.is_active),
        )
        strategy = effective_strategy_from_ai_config(snapshot)
        await db.commit()
        return snapshot, strategy


def decision_passes_strategy(decision: Any, strategy: StrategyConfig) -> tuple[bool, str]:
    if not decision or not getattr(decision, "should_bet", False):
        return False, "未通过组合闸门"
    odds = float(getattr(decision, "odds", 0) or 0)
    if not (float(strategy.min_odds) <= odds <= float(strategy.max_odds or 99)):
        return False, "赔率已超出最新配置"
    if float(getattr(decision, "confidence", 0) or 0) < float(strategy.min_confidence):
        return False, "最终概率低于最新配置"
    if float(getattr(decision, "suggested_stake", 0) or 0) < AI_MIN_STAKE:
        return False, "仓位需 ≥1"
    return True, ""


class BetDecision(BaseModel):
    match_id: int
    selection: str
    confidence: float
    suggested_stake: Decimal
    reasoning: str
    risk_score: float
    should_bet: bool
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
    """生产唯一策略引擎，不保留旧测试/兼容规则。"""

    def __init__(self, config: Optional[StrategyConfig] = None, user_id: Optional[int] = None):
        self.config = config or DEFAULT_STRATEGY
        self.user_id = user_id

    async def evaluate_bet(
        self,
        match_info: dict,
        analysis: dict,
        user_balance: Decimal,
        daily_loss: Decimal,
        active_bets_count: int,
    ) -> BetDecision:
        prediction = str(analysis.get("prediction") or "").lower()
        bet_type = str(analysis.get("bet_type") or "total").lower()
        if bet_type != "total" or prediction not in ("under", "over"):
            return self._reject(match_info, analysis, "组合闸门[data]: 仅允许全场大小球under/over")
        reasoning = str(analysis.get("reasoning") or "")
        if not analysis.get("consensus_reached") or "[不投注]" in reasoning or "不可下单" in reasoning:
            return self._reject(match_info, analysis, "组合闸门[data]: AI共识不足或已标记不可下单")

        from app.services.bookmakers.match_live import match_elapsed_seconds, parse_match_clock_minutes

        sport = str(match_info.get("sport") or "").lower()
        league = str(match_info.get("league") or "")
        line = _as_float(analysis.get("line", match_info.get("total_line")), None)
        confidence = max(0.0, min(0.99, _as_float(analysis.get("confidence"), 0.0)))
        odds_map = match_info.get("odds") if isinstance(match_info.get("odds"), dict) else {}
        odds = _as_float(odds_map.get(prediction) or analysis.get("odds"), 0.0)
        elapsed = match_elapsed_seconds(
            sport=sport, period=str(match_info.get("period") or ""),
            clock=str(match_info.get("clock") or ""), league=league,
        )
        played = elapsed / 60 if elapsed is not None else parse_match_clock_minutes(
            str(match_info.get("clock") or ""), allow_countdown=False,
        )
        result = evaluate_balanced_gate(
            match_info=match_info, analysis=analysis, selection=prediction,
            confidence=confidence, odds=odds, line=line, played_minutes=played,
            configured_min_confidence=float(self.config.min_confidence or 0),
        )
        analysis["required_confidence"] = result.required_confidence
        analysis["remaining_score_probability"] = result.remaining_score_probability
        analysis["balanced_gate"] = {
            "allowed": result.allowed, "gate": result.gate, "reason": result.reason,
            "required_confidence": result.required_confidence,
            "remaining_score_probability": result.remaining_score_probability,
        }
        if not result.allowed:
            logger.info(
                "[组合闸门/%s] ❌ match=%s final=%.2f required=%.2f remaining=%s | %s",
                result.gate, match_info.get("id"), confidence, result.required_confidence,
                f"{result.remaining_score_probability:.1%}" if result.remaining_score_probability is not None else "n/a",
                result.reason,
            )
            return self._reject(match_info, analysis, f"组合闸门[{result.gate}]: {result.reason}")

        stake = self._stake(confidence, result.required_confidence, user_balance, daily_loss)
        risk = self._risk_score(confidence, odds, active_bets_count)
        logger.info(
            "[组合闸门/pass] ✅ match=%s sel=%s final=%.2f required=%.2f remaining=%.1f%% odds=%.2f stake=%.2f",
            match_info.get("id"), prediction, confidence, result.required_confidence,
            float(result.remaining_score_probability or 0) * 100, odds, float(stake),
        )
        return BetDecision(
            match_id=int(match_info.get("id") or 0), selection=prediction, confidence=confidence,
            suggested_stake=stake, reasoning=reasoning or result.reason, risk_score=risk,
            should_bet=stake >= Decimal(str(AI_MIN_STAKE)),
            provider_code=str(analysis.get("provider_code") or match_info.get("provider_code") or ""),
            odds=odds, line=line, sport=sport, period=str(match_info.get("period") or ""),
            clock=str(match_info.get("clock") or ""),
            home_score=int(_as_float(match_info.get("home_score"), 0)),
            away_score=int(_as_float(match_info.get("away_score"), 0)),
        )

    def _stake(self, confidence: float, required: float, balance: Decimal, daily_loss: Decimal) -> Decimal:
        factor = 0.55 + 0.35 * min(1.0, max(0.0, confidence - required) / max(0.01, 0.90 - required))
        stake = Decimal(str(self.config.max_bet_amount or 1)) * Decimal(str(round(factor, 3)))
        if Decimal(str(balance or 0)) > 0:
            stake = min(stake, Decimal(str(balance)) * Decimal("0.25"))
        if float(self.config.stop_loss or 0) > 0 and float(daily_loss or 0) > 0:
            ratio = min(float(daily_loss) / float(self.config.stop_loss), 1.0)
            stake *= Decimal(str(round(1.0 - 0.5 * ratio, 3)))
        return min(max(Decimal("0"), stake.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)), Decimal(str(self.config.max_bet_amount)))

    @staticmethod
    def _risk_score(confidence: float, odds: float, active_count: int) -> float:
        risk = (1 - confidence) * float(settings.AI_RISK_LOW_CONF_WEIGHT)
        risk += float(settings.AI_RISK_HIGH_ODDS_PENALTY) if odds > 1.90 else float(settings.AI_RISK_MID_ODDS_PENALTY) if odds > 1.80 else 0
        return round(min(max(risk + min(max(active_count, 0), 5) * 0.03, 0), 1), 4)

    @staticmethod
    def _reject(match_info: dict, analysis: dict, reason: str) -> BetDecision:
        confidence = _as_float(analysis.get("confidence"), 0)
        odds = _as_float(analysis.get("odds"), 0)
        logger.info("❌ 策略拒绝 | match=%s sel=%s final=%.2f | %s", match_info.get("id"), analysis.get("prediction"), confidence, reason)
        return BetDecision(
            match_id=int(match_info.get("id") or 0), selection=str(analysis.get("prediction") or "under"),
            confidence=confidence, suggested_stake=Decimal("0"), reasoning=f"[不投注] {reason}",
            risk_score=1, should_bet=False,
            provider_code=str(analysis.get("provider_code") or match_info.get("provider_code") or ""),
            odds=odds, line=_as_float(analysis.get("line"), None), sport=str(match_info.get("sport") or ""),
            period=str(match_info.get("period") or ""), clock=str(match_info.get("clock") or ""),
            home_score=int(_as_float(match_info.get("home_score"), 0)), away_score=int(_as_float(match_info.get("away_score"), 0)),
        )
