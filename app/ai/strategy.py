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

    min_confidence: float = settings.AI_MIN_CONFIDENCE    # 最低AI置信度
    min_odds: float = settings.AI_MIN_ODDS                  # 最低赔率
    max_odds: Optional[float] = settings.AI_MAX_ODDS        # 最高赔率


DEFAULT_STRATEGY = StrategyConfig(name="simple")
AI_MIN_STAKE = 1.0


def effective_strategy_from_ai_config(ai_config) -> StrategyConfig:
    """以 simple 预设为底，覆盖用户在配置里保存的数值。"""
    if ai_config is None:
        return DEFAULT_STRATEGY.model_copy(deep=True)

    base = DEFAULT_STRATEGY.model_copy(deep=True)

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


def _list_attr(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(x).strip() for x in value if str(x).strip()]
    return []


def ai_config_response_payload(ai_config: Any | None) -> dict[str, Any]:
    """统一 AI 配置对外返回结构，避免前后端各自拼默认值。"""
    effective = (
        effective_strategy_from_ai_config(ai_config)
        if ai_config is not None
        else DEFAULT_STRATEGY.model_copy(deep=True)
    )
    scan_interval_sec = max(120, int(getattr(settings, "AI_SCAN_INTERVAL_SEC", 120) or 120))
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
        "min_confidence": float(effective.min_confidence),
        "min_odds": float(effective.min_odds),
        "max_odds": float(effective.max_odds),
        "min_bet_amount": AI_MIN_STAKE,
        "runtime_limits": {
            "scan_interval_sec": scan_interval_sec,
            "scan_interval_min": round(scan_interval_sec / 60, 2),
            "stream_bet_mode": True,
        },
    }


def _as_float(value: Any, default: Any = 0.0) -> Any:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
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
            return None, DEFAULT_STRATEGY.model_copy(deep=True)
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
        self.config = config or DEFAULT_STRATEGY

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
        sport = match_info.get("sport", "?")
        conf_f = float(confidence or 0)
        min_conf = float(self.config.min_confidence or 0.0)
        min_odds_cfg = float(self.config.min_odds)
        max_odds_cfg = _as_float(self.config.max_odds, 99.0)
        odds_data = match_info.get("odds", {}) or {}

        # ── 入口日志：打印全部配置阈值 + AI 原始分析 ──
        logger.info(
            "[闸门入口] match=%s %s %s vs %s | AI方向=%s 置信度=%.4f | "
            "配置阈值: 最低胜率=%.2f 最低赔率=%.2f 最高赔率=%.2f | "
            "余额=%.2f 日亏=%.2f 活跃单=%d",
            mid, sport, ht, at, prediction, conf_f,
            min_conf, min_odds_cfg, max_odds_cfg,
            float(user_balance), float(daily_loss), active_bets_count,
        )

        # ── 闸门1：方向检查 ──
        if prediction not in ("over", "under"):
            logger.info(
                "[闸门1/方向] ❌ 拒绝 match=%s | AI方向='%s' 不在 [over, under] 内 | "
                "analysis.prediction='%s' analysis全部=%s",
                mid, prediction,
                analysis.get("prediction", ""), {k: analysis.get(k) for k in ("prediction", "confidence", "odds") if k in analysis},
            )
            return self._reject(match_info, analysis, f"不支持的投注方向: {prediction}")
        logger.info(
            "[闸门1/方向] ✅ 通过 match=%s | 方向=%s（大球over/小球under）",
            mid, prediction,
        )

        # ── 闸门1.5：硬保护，任何“不可下单”文案或共识失败一律拦截 ──
        reasoning = str(analysis.get("reasoning") or "")
        consensus_reached = bool(analysis.get("consensus_reached", False))
        if (not consensus_reached) or ("[不投注]" in reasoning) or ("不可下单" in reasoning):
            logger.info(
                "[闸门1.5/硬保护] ❌ 拒绝 match=%s | consensus=%s | reasoning=%s",
                mid, consensus_reached, reasoning[:220],
            )
            why = "模型未通过下单门禁"
            if "[不投注]" in reasoning:
                why = "文案已标记为不投注"
            elif "不可下单" in reasoning:
                why = "文案已标记为不可下单"
            elif not consensus_reached:
                why = "consensus_reached=false"
            return self._reject(match_info, analysis, why)
        logger.info(
            "[闸门1.5/硬保护] ✅ 通过 match=%s | consensus=%s",
            mid, consensus_reached,
        )

        # ── 闸门2：置信度检查（参考，实际由闸门2.7按方向分别判断）──
        if conf_f < min_conf:
            logger.info(
                "[闸门2/置信度] 参考 match=%s | 实际=%.4f 低于通用阈值=%.4f | 将由闸门2.7按方向判断",
                mid, conf_f, min_conf,
            )
        else:
            logger.info(
                "[闸门2/置信度] ✅ 通过 match=%s | 实际=%.4f ≥ 阈值=%.4f",
                mid, conf_f, min_conf,
            )

        # ── 闸门2.5：初指 + 实时盘口 + 基本面 三重门禁 ──
        signal_review = analysis.get("signal_review") if isinstance(analysis.get("signal_review"), dict) else {}
        triad_ready = bool(signal_review.get("triad_ready"))
        verdict = str(signal_review.get("verdict") or "").strip().lower()
        edge_score = _as_float(signal_review.get("edge_score"), 0.0)
        # 单模型模式：三重门禁仅作参考标注，不阻断 GPT 的 over/under 判断
        logger.info(
            "[闸门2.5/三重门禁] 参考（不阻断） match=%s | triad_ready=%s verdict=%s edge=%.2f",
            mid, triad_ready, verdict, edge_score,
        )

        # ── 闸门2.6：足球大球保守保护（近期实盘大球回撤高，先收紧） ──
        sport_l = str(sport or "").lower().strip()
        total_line = _as_float(
            analysis.get("line", match_info.get("total_line", match_info.get("line"))),
            None,
        )
        home_score = _as_int(match_info.get("home_score"), 0)
        away_score = _as_int(match_info.get("away_score"), 0)
        current_total = home_score + away_score
        conflict_points = _as_int(signal_review.get("conflict_points"), 0)
        market_points = _as_int(signal_review.get("market_points"), 0)
        fundamental_points = _as_int(signal_review.get("fundamental_points"), 0)
        # 单模型模式：足球大球保护降级为参考日志，不阻断 GPT 判断
        played_mins = None
        try:
            from app.services.bookmakers.match_live import parse_match_clock_minutes

            played_mins = parse_match_clock_minutes(
                str(match_info.get("clock") or "").strip(),
                allow_countdown=False,
            )
        except Exception:
            played_mins = None

        if sport_l == "football" and prediction == "over":
            logger.info(
                "[闸门2.6/足球大球保护] 参考（不阻断） match=%s | conf=%.4f line=%s total=%d mins=%s",
                mid, conf_f, total_line, current_total, played_mins,
            )

        # ── 闸门2.7：双向风控（按运动类型分离参数）──
        ctx_source = str(analysis.get("context_source") or "none").strip().lower()
        has_fundamentals = ctx_source not in ("", "none")

        # 运动类型风控参数
        if sport_l == "basketball":
            RISK = {
                "over_no_fundamentals": True,      # 无基本面禁止 over
                "over_max_line": 200.0,             # over 最大盘线
                "over_min_conf": 0.40,              # over 最低置信度
                "under_min_conf": 0.30,             # under 最低置信度（有基本面）
                "under_min_conf_no_fund": 0.40,     # under 最低置信度（无基本面）
                "under_max_line": 210.0,            # under 最大盘线（加时/罚球变数大）
            }
        else:  # football
            RISK = {
                "over_no_fundamentals": True,
                "over_max_line": 3.0,
                "over_min_conf": 0.40,
                "under_min_conf": 0.30,
                "under_min_conf_no_fund": 0.40,
                "under_max_line": None,             # 足球 under 无高线限制
                "under_min_line": 1.5,              # 足球低线 under 1球破盘
            }

        if prediction == "over":
            if RISK["over_no_fundamentals"] and not has_fundamentals:
                logger.info("[闸门2.7/over风控] ❌ 拒绝 match=%s | 无基本面数据，over方向禁止", mid)
                return self._reject(match_info, analysis, "无基本面数据支撑，over方向禁止下单")
            if total_line is not None and total_line >= RISK["over_max_line"]:
                logger.info("[闸门2.7/over风控] ❌ 拒绝 match=%s | %s高线over line=%.2f>=%.2f",
                            mid, sport_l, total_line, RISK["over_max_line"])
                return self._reject(match_info, analysis, f"{sport_l}高线over（line={total_line:.2f}）历史胜率极低")
            over_min_conf = max(min_conf, RISK["over_min_conf"])
            if conf_f < over_min_conf:
                logger.info("[闸门2.7/over风控] ❌ 拒绝 match=%s | over置信度=%.4f < 要求=%.4f", mid, conf_f, over_min_conf)
                return self._reject(match_info, analysis, f"over方向需更高置信度（当前{conf_f:.2f}，要求{over_min_conf:.2f}）")

        elif prediction == "under":
            under_min_conf = RISK["under_min_conf"] if has_fundamentals else RISK["under_min_conf_no_fund"]
            if conf_f < under_min_conf:
                logger.info("[闸门2.7/under风控] ❌ 拒绝 match=%s | under置信度=%.4f < 要求=%.4f (fundamentals=%s)",
                            mid, conf_f, under_min_conf, has_fundamentals)
                return self._reject(match_info, analysis, f"under置信度不足（当前{conf_f:.2f}，要求{under_min_conf:.2f}）")
            if sport_l == "football" and total_line is not None and total_line <= RISK.get("under_min_line", 1.5):
                logger.info("[闸门2.7/under风控] ❌ 拒绝 match=%s | 足球低线under line=%.2f，1球即破盘", mid, total_line)
                return self._reject(match_info, analysis, f"足球低线under（line={total_line:.2f}）1球即破盘，风险过高")
            if sport_l == "basketball" and total_line is not None and RISK.get("under_max_line") and total_line >= RISK["under_max_line"]:
                logger.info("[闸门2.7/under风控] ❌ 拒绝 match=%s | 篮球高线under line=%.1f>=%.1f，变数大", mid, total_line, RISK["under_max_line"])
                return self._reject(match_info, analysis, f"篮球高线under（line={total_line:.1f}）加时/罚球变数大")

        # ── 闸门2.8：盘口变化方向过滤 ──
        line_moves_raw = match_info.get("line_movements") or match_info.get("line_movement") or {}
        total_move = None
        if isinstance(line_moves_raw, dict):
            # line_movements 是 dict（key=bet_type）
            total_move = line_moves_raw.get("total") or line_moves_raw
        elif isinstance(line_moves_raw, list) and line_moves_raw:
            total_move = line_moves_raw[-1] if isinstance(line_moves_raw[-1], dict) else {}

        mkt_support = "neutral"
        mkt_strength = "none"
        if isinstance(total_move, dict) and total_move:
            line_delta = total_move.get("line_delta")
            if line_delta is not None:
                try:
                    ld = float(line_delta)
                    if ld <= -0.25:
                        mkt_support = "under"
                        mkt_strength = "strong" if abs(ld) >= 0.5 else "medium"
                    elif ld >= 0.25:
                        mkt_support = "over"
                        mkt_strength = "strong" if abs(ld) >= 0.5 else "medium"
                except (TypeError, ValueError):
                    pass

        if mkt_support != "neutral" and mkt_support != prediction:
            # 市场方向与 GPT 预测相反
            logger.info(
                "[闸门2.8/盘口方向] ❌ 拒绝 match=%s | 市场支持%s 但GPT预测%s | line_delta=%s strength=%s",
                mid, mkt_support, prediction, total_move.get("line_delta") if isinstance(total_move, dict) else "?", mkt_strength,
            )
            return self._reject(match_info, analysis, f"盘口变化方向({mkt_support})与预测({prediction})相反")
        logger.info(
            "[闸门2.8/盘口方向] ✅ 通过 match=%s | 市场支持=%s 预测=%s strength=%s",
            mid, mkt_support, prediction, mkt_strength,
        )

        # ── 闸门3：赔率区间检查 ──
        odds_raw_over = odds_data.get("over")
        odds_raw_under = odds_data.get("under")
        odds_from_analysis = analysis.get("odds")
        try:
            odds = float(odds_data.get(prediction) or analysis.get("odds") or 0)
        except (TypeError, ValueError):
            odds = 0.0
        odds_source = "odds_data" if odds_data.get(prediction) else ("analysis" if odds_from_analysis else "无来源")
        logger.info(
            "[闸门3/赔率] match=%s | 取值=%.4f 来源=%s | "
            "odds_data={over:%s, under:%s} analysis.odds=%s | "
            "区间[%.2f, %.2f]",
            mid, odds, odds_source,
            odds_raw_over, odds_raw_under, odds_from_analysis,
            min_odds_cfg, max_odds_cfg,
        )
        if odds <= 1.0:
            logger.info(
                "[闸门3/赔率] ❌ 拒绝 match=%s | 赔率=%.4f 无效(≤1.0) | 来源=%s | odds_data=%s analysis.odds=%s",
                mid, odds, odds_source, odds_data, odds_from_analysis,
            )
            return self._reject(match_info, analysis, f"赔率无效: {odds}")
        if odds < min_odds_cfg:
            logger.info(
                "[闸门3/赔率] ❌ 拒绝 match=%s | 赔率=%.4f < 最低=%.2f | 差距=%.4f | 来源=%s",
                mid, odds, min_odds_cfg, min_odds_cfg - odds, odds_source,
            )
            return self._reject(match_info, analysis, f"赔率 {odds} 低于最低 {min_odds_cfg}")
        if odds > max_odds_cfg:
            logger.info(
                "[闸门3/赔率] ❌ 拒绝 match=%s | 赔率=%.4f > 最高=%.2f | 超出=%.4f | 来源=%s",
                mid, odds, max_odds_cfg, odds - max_odds_cfg, odds_source,
            )
            return self._reject(match_info, analysis, f"赔率 {odds} 高于最高 {max_odds_cfg}")
        logger.info(
            "[闸门3/赔率] ✅ 通过 match=%s | 赔率=%.4f 在区间[%.2f, %.2f]内 | 来源=%s",
            mid, odds, min_odds_cfg, max_odds_cfg, odds_source,
        )

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


# 全局策略引擎
strategy_engine = StrategyEngine(DEFAULT_STRATEGY)
