"""
AI 投注策略引擎

简化模式：仅按 AI 分析给出的小球概率进行下注
"""
from __future__ import annotations

import logging
from decimal import Decimal, ROUND_HALF_UP
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

# ── 运动类型风控参数（单一配置源：闸门2.7 双向风控 + 闸门2.9 under余量）──
SPORT_RISK: dict[str, dict] = {
    "basketball": {
        "under_min_conf": 0.58,            # 篮球小分波动更大：有基本面也需更高置信度
        "under_min_conf_no_fund": 0.62,    # 无基本面时再加严
        "under_min_line": 120.0,           # 篮球小球盘线区间下限（低于此线不投）
        "under_max_line": 208.0,           # 高线小分容错极低，提前拦截
        "under_min_played_mins": 14.0,     # 首节/次节早段样本太小，不做小分
        "under_late_block_mins": 44.0,     # 末节最后 4 分钟犯规/罚球波动极大
        # under 余量（全场48分钟按盘口线等比折算剩余期望）
        "margin_min_mins": 24.0,           # 中盘后才看余量
        "margin_full_mins": 48.0,
        "margin_avg_goals": None,          # None=按盘口线折算
        "margin_factor": 1.45,
        "late_margin_floor": 4.0,          # 末节/加时余量过薄时一波罚球就破盘
        "ev_conf_edge": 0.04,              # 小分必须明显高于盈亏平衡概率
    },
    "football": {
        "under_min_conf": 0.55,            # under 最低置信度
        "under_min_conf_no_fund": 0.58,  # under 无基本面加严
        "under_min_line": 2.0,             # 足球低线 under 1球破盘
        "under_max_line": 6.5,             # 足球 under 高线限制
        "under_min_played_mins": 20.0,
        "under_late_block_mins": 90.0,
        # under 余量（全场90分钟按联赛均值2.75球折算剩余期望）
        "margin_min_mins": 40.0,
        "margin_full_mins": 90.0,
        "margin_avg_goals": 2.75,
        "margin_factor": 1.3,
        "late_margin_floor": 0.5,
        "ev_conf_edge": 0.0,
    },
}
# 兜底：未知运动按足球参数处理（注意：深拷贝避免 default 与 football 同对象互改）
SPORT_RISK["default"] = {k: dict(v) if isinstance(v, dict) else v for k, v in SPORT_RISK["football"].items()}

# 联赛黑名单关键词（实盘教训：青少年/女子联赛进球极不稳定，2026-08-14 该类5注仅1胜）
# 同时用于：B2 下单闸门（strategy.evaluate_bet）+ 扫描层前置过滤（analysis_filters.skip_reason_for_match）
LEAGUE_BLACKLIST_KEYWORDS: tuple[str, ...] = (
    "u19", "u21", "u18", "u20", "u17", "u16",
    "青年", "青少年", "后备队", "女子", "(女)", "women", "女篮",
    "友谊赛", "表演赛",
)


def league_is_blacklisted(league: str) -> bool:
    """联赛名是否命中黑名单（青少年/女子等进球不稳定赛事）。"""
    if not league:
        return False
    league_l = str(league).lower()
    return any(kw in league_l for kw in LEAGUE_BLACKLIST_KEYWORDS)


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
    """AI 投注决策（仅小球）"""
    match_id: int
    selection: str  # under（仅小球）
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
    策略引擎 - 仅按 AI 分析给出的小球概率进行下注
    """

    def __init__(self, config: Optional[StrategyConfig] = None, user_id: Optional[int] = None):
        self.config = config or DEFAULT_STRATEGY
        self.user_id = user_id  # 胜率自适应统计按用户隔离（多用户互不污染门槛）
        self._cached_stats: dict | None = None  # 胜率统计每轮缓存一次，避免逐场查 Redis

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

        五阶段闸门链（A信号有效性 → B结构性风控 → C市场一致性 → D滚球余量 → E赔率有效性）。
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

        # ══════════════════════════════════════════════════════════
        # 阶段A：信号有效性（AI 输出是否可执行）
        #   A0 玩法白名单 + A1 方向合法 + A2 模型共识 + A3 置信度达标（含按方向/基本面分级）
        # ══════════════════════════════════════════════════════════

        # ── A0：玩法白名单（仅全场小球 total/under，其他一律拒绝）──
        raw_bet_type = str(analysis.get("bet_type", "") or "").lower()
        if raw_bet_type and raw_bet_type not in ("total", "first_half_total", "second_half_total"):
            logger.info(
                "[A0/玩法] ❌ 拒绝 match=%s | bet_type='%s' 不在白名单(仅 total/first_half_total/second_half_total)",
                mid, raw_bet_type,
            )
            return self._reject(match_info, analysis, f"不支持的玩法: {raw_bet_type}（仅全场/半场小球）")

        # ── A1：方向检查 ──
        if prediction != "under":
            logger.info(
                "[A1/方向] ❌ 拒绝 match=%s | AI方向='%s' 不是小球",
                mid, prediction,
            )
            return self._reject(match_info, analysis, f"不支持的投注方向: {prediction}")

        # ── A2：模型共识硬保护 ──
        reasoning = str(analysis.get("reasoning") or "")
        consensus_reached = bool(analysis.get("consensus_reached", False))
        if not consensus_reached or "[不投注]" in reasoning or "不可下单" in reasoning:
            why = ("文案已标记为不投注" if "[不投注]" in reasoning
                   else "文案已标记为不可下单" if "不可下单" in reasoning
                   else "consensus_reached=false")
            logger.info("[A2/共识] ❌ 拒绝 match=%s | consensus=%s | why=%s", mid, consensus_reached, why)
            return self._reject(match_info, analysis, why)

        # ── A3：置信度（用户 AI 配置主导 + 无基本面加严 + 胜率自适应）──
        sport_l = str(sport or "").lower().strip()
        RISK = SPORT_RISK.get(sport_l, SPORT_RISK["default"])
        ctx_source = str(analysis.get("context_source") or "none").strip().lower()
        has_fundamentals = ctx_source not in ("", "none")
        total_line = _as_float(
            analysis.get("line", match_info.get("total_line", match_info.get("line"))),
            None,
        )
        home_score = _as_int(match_info.get("home_score"), 0)
        away_score = _as_int(match_info.get("away_score"), 0)
        current_total = home_score + away_score
        played_mins = None
        try:
            from app.services.bookmakers.match_live import (
                match_elapsed_seconds,
                parse_match_clock_minutes,
            )

            # 篮球时钟是「节内倒计时」，parse_match_clock_minutes 会直接丢弃 →
            # D1 余量闸门对篮球倒计时时钟整体失效，改用节次换算的已进行秒数。
            elapsed_secs = match_elapsed_seconds(
                sport=sport_l,
                period=str(match_info.get("period") or ""),
                clock=str(match_info.get("clock") or "").strip(),
            )
            if elapsed_secs is not None:
                played_mins = elapsed_secs / 60.0
            else:
                played_mins = parse_match_clock_minutes(
                    str(match_info.get("clock") or "").strip(),
                    allow_countdown=False,
                )
        except Exception:
            played_mins = None

        # 有效置信度门槛 = 用户配置（>0时） → 无基本面时取两者较大 → over再叠地板 → 胜率自适应再加成
        user_min_conf = min_conf if min_conf > 0 else 0.0
        if user_min_conf > 0:
            base_req = user_min_conf
        else:
            base_req = RISK["under_min_conf"]  # 未配置时的兜底地板
        if not has_fundamentals:
            base_req = max(base_req, RISK["under_min_conf_no_fund"])

        adaptive_bump = 0.0
        try:
            if self._cached_stats is None:
                from app.services.bet_settlement import recent_betting_stats

                self._cached_stats = await recent_betting_stats(days=7, user_id=self.user_id)
            stats = self._cached_stats
            settled_n = int(stats.get("settled") or 0)
            win_rate = stats.get("win_rate")
            if settled_n >= 5 and isinstance(win_rate, (int, float)):
                if win_rate < 0.35:
                    adaptive_bump = 0.10
                elif win_rate < 0.45:
                    adaptive_bump = 0.05
                # 注意：不做高胜率放宽。实盘教训（2026-08-15~16）：放宽 -0.05 后
                # under conf<0.60 的单涌入（8单1胜），整体胜率 69%→30%。
                # 高胜率是高门槛的结果，放宽门槛即摧毁胜率。
            if adaptive_bump:
                logger.info(
                    "[A3/置信度] 胜率自适应 match=%s | 近7天%d结算胜率%.1f%%<45%% | 门槛+%.2f",
                    mid, settled_n, win_rate * 100, adaptive_bump,
                )
            else:
                logger.info(
                    "[A3/置信度] 胜率自适应跳过 match=%s | 样本%d结算/胜率正常",
                    mid, settled_n,
                )
        except Exception as e:
            logger.warning("[A3/胜率自适应] 统计加载失败(跳过): %s", e)

        required_conf = base_req + adaptive_bump
        if conf_f < required_conf:
            logger.info(
                "[A3/置信度] ❌ 拒绝 match=%s | conf=%.4f < 要求=%.4f (用户配置=%.2f fundamentals=%s 自适应+%.2f)",
                mid, conf_f, required_conf, user_min_conf, has_fundamentals, adaptive_bump,
            )
            return self._reject(
                match_info, analysis,
                f"{prediction}置信度不足（当前{conf_f:.2f}，要求{required_conf:.2f}）",
            )
        logger.info(
            "[A3/置信度] ✅ 通过 match=%s | conf=%.2f ≥ 要求=%.2f | 方向=%s fundamentals=%s",
            mid, conf_f, required_conf, prediction, has_fundamentals,
        )

        review = analysis.get("signal_review") if isinstance(analysis.get("signal_review"), dict) else {}
        if sport_l == "basketball":
            triad_ready = bool(review.get("triad_ready"))
            review_verdict = str(review.get("verdict") or "")
            market_points = _as_int(review.get("market_points"), 0)
            fundamental_points = _as_int(review.get("fundamental_points"), 0)
            conflict_points = _as_int(review.get("conflict_points"), 0)
            if not triad_ready:
                logger.info("[A4/篮球三重门禁] ❌ 拒绝 match=%s | triad_ready=false review=%s", mid, review)
                return self._reject(match_info, analysis, "篮球缺少 初指+实时盘口+基本面 三重门禁，不放行")
            if review_verdict == "conflict" or conflict_points > 1:
                logger.info(
                    "[A4/篮球三重门禁] ❌ 拒绝 match=%s | verdict=%s conflict=%d",
                    mid, review_verdict, conflict_points,
                )
                return self._reject(match_info, analysis, "篮球结构化复核冲突过多，不放行")
            if market_points < 3 or fundamental_points < 3:
                logger.info(
                    "[A4/篮球三重门禁] ❌ 拒绝 match=%s | market=%d fundamental=%d 不足",
                    mid, market_points, fundamental_points,
                )
                return self._reject(match_info, analysis, "篮球盘口/基本面支持不足，不放行")
            logger.info(
                "[A4/篮球三重门禁] ✅ 通过 match=%s | verdict=%s market=%d fundamental=%d conflict=%d",
                mid, review_verdict, market_points, fundamental_points, conflict_points,
            )

        # ══════════════════════════════════════════════════════════
        # 阶段B：结构性风控（盘口线本身的下注价值）
        #   B1 小球盘线区间
        # ══════════════════════════════════════════════════════════

        if sport_l == "football" and played_mins is not None and played_mins < float(RISK.get("under_min_played_mins", 20.0)):
                # 近7天本地已结算样本（2026-08-10~2026-08-17）：
                # - 足球 under 全样本 13胜11负，WR 54.2%
                # - 加上现有 line>2.0 过滤后，若再去掉 20' 前早段单，提升到 8胜2负，WR 80.0%
                # 早段 under 最大问题：样本太小，1次快攻/点球/红牌就能把小球彻底打穿。
                logger.info(
                    "[B1/under风控] ❌ 拒绝 match=%s | 足球under早段不下 mins=%.1f<20",
                    mid, played_mins,
                )
                return self._reject(
                    match_info,
                    analysis,
                    f"足球under前20分钟样本过小（{played_mins:.0f}'），保护性跳过",
                )
        if sport_l == "football" and total_line is not None and total_line <= RISK.get("under_min_line", 1.5):
                logger.info("[B1/under风控] ❌ 拒绝 match=%s | 足球低线under line=%.2f，1球即破盘", mid, total_line)
                return self._reject(match_info, analysis, f"足球低线under（line={total_line:.2f}）1球即破盘，风险过高")
        if sport_l == "basketball" and played_mins is not None and played_mins < float(RISK.get("under_min_played_mins", 14.0)):
                logger.info(
                    "[B1/under风控] ❌ 拒绝 match=%s | 篮球under早段不下 mins=%.1f<%.1f",
                    mid, played_mins, float(RISK.get("under_min_played_mins", 14.0)),
                )
                return self._reject(
                    match_info,
                    analysis,
                    f"篮球under前{int(float(RISK.get('under_min_played_mins', 14.0)))}分钟样本过小，保护性跳过",
                )
        if sport_l == "basketball" and total_line is not None and RISK.get("under_max_line") and total_line >= RISK["under_max_line"]:
                logger.info(
                    "[B1/under风控] ❌ 拒绝 match=%s | 篮球高线under line=%.1f>=%.1f，变数大",
                    mid, total_line, RISK["under_max_line"],
                )
                return self._reject(match_info, analysis, f"篮球高线under（line={total_line:.1f}）加时/罚球变数大")
        if sport_l == "basketball" and played_mins is not None and played_mins >= float(RISK.get("under_late_block_mins", 44.0)):
                logger.info(
                    "[B1/under风控] ❌ 拒绝 match=%s | 篮球under末节最后阶段不下 mins=%.1f≥%.1f",
                    mid, played_mins, float(RISK.get("under_late_block_mins", 44.0)),
                )
                return self._reject(
                    match_info,
                    analysis,
                    f"篮球under进入末节高犯规时段（{played_mins:.0f}'），保护性跳过",
                )

        # ── B2：联赛质量闸门（关键词见模块级 LEAGUE_BLACKLIST_KEYWORDS，扫描层已前置过滤，此处兜底）──
        league = str(match_info.get("league") or analysis.get("league") or "")
        if league_is_blacklisted(league):
            logger.info(
                "[B2/联赛质量] ❌ 拒绝 match=%s | 青少年/女子联赛进球不稳定 league=%s",
                mid, league,
            )
            return self._reject(
                match_info, analysis,
                f"联赛类型风控（{league}：青少年/女子赛事进球波动大）",
            )

        # ── B3：高赔率 under 风险（under 正常水位≤1.95，≥2.0 说明市场强烈看大）──
        try:
            sel_odds_f = float(odds_data.get(prediction) or analysis.get("odds") or 0)
        except (TypeError, ValueError):
            sel_odds_f = 0.0
        if prediction == "under" and sel_odds_f >= 2.0:
            logger.info(
                "[B3/高赔率under] ❌ 拒绝 match=%s | under赔率=%.2f ≥2.0 市场强烈看大",
                mid, sel_odds_f,
            )
            return self._reject(
                match_info, analysis,
                f"小球赔率过高（{sel_odds_f:.2f}，不符合风险要求）",
            )

        # ══════════════════════════════════════════════════════════
        # 阶段C：市场一致性（盘口变化方向）
        #   C1 市场降/升盘方向与预测相反 → 拒绝
        # ══════════════════════════════════════════════════════════

        line_moves_raw = match_info.get("line_movements") or match_info.get("line_movement") or {}
        total_move = None
        if isinstance(line_moves_raw, dict):
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
                        mkt_support = "against_under"
                        mkt_strength = "strong" if abs(ld) >= 0.5 else "medium"
                except (TypeError, ValueError):
                    pass

        if mkt_support != "neutral" and mkt_support != prediction:
            logger.info(
                "[C1/盘口方向] ❌ 拒绝 match=%s | 市场支持%s 但AI预测%s | strength=%s",
                mid, mkt_support, prediction, mkt_strength,
            )
            return self._reject(match_info, analysis, f"盘口变化方向({mkt_support})与预测({prediction})相反")
        if sport_l == "basketball" and prediction == "under" and mkt_support != "under":
            logger.info(
                "[C1/盘口方向] ❌ 拒绝 match=%s | 篮球under要求市场同步支持，当前=%s",
                mid, mkt_support,
            )
            return self._reject(match_info, analysis, "篮球under缺少降盘/水位同步支持，不放行")
        logger.info(
            "[C1/盘口方向] ✅ 通过 match=%s | 市场支持=%s 预测=%s strength=%s",
            mid, mkt_support, prediction, mkt_strength,
        )

        # ══════════════════════════════════════════════════════════
        # 阶段D：滚球余量（仅 under，中后段余量不足以覆盖剩余期望 → 拒绝）
        #   实盘教训：bet58 下48' 1球押 under2.5，终场3球破线
        # ══════════════════════════════════════════════════════════

        if prediction == "under" and total_line is not None and played_mins is not None:
            margin = total_line - current_total
            m_cfg = RISK
            if m_cfg["margin_min_mins"] <= played_mins < m_cfg["margin_full_mins"]:
                base = m_cfg["margin_avg_goals"] if m_cfg["margin_avg_goals"] else total_line
                expected_remaining = (m_cfg["margin_full_mins"] - played_mins) / m_cfg["margin_full_mins"] * base
                if margin < expected_remaining * m_cfg["margin_factor"]:
                    logger.info(
                        "[D1/under余量] ❌ 拒绝 match=%s | %s %.0f' 余量%.2f < 剩余期望%.2f×%.2f",
                        mid, sport_l, played_mins, margin, expected_remaining, m_cfg["margin_factor"],
                    )
                    return self._reject(
                        match_info, analysis,
                        f"{sport_l} under余量不足（{played_mins:.0f}'剩{margin:.2f}，期望还需{expected_remaining:.2f}）",
                    )
            elif played_mins >= m_cfg["margin_full_mins"]:
                # 补时/加时段（原 [min, full) 开区间在此段整体跳过）：
                # 余量过薄（足球≤0.5球 / 篮球≤2分）时任何一次得分即破盘，拒绝。
                late_margin_floor = float(m_cfg.get("late_margin_floor", 0.5 if sport_l == "football" else 2.0))
                if margin <= late_margin_floor:
                    logger.info(
                        "[D1/under余量] ❌ 拒绝 match=%s | %s %.0f'≥%.0f' 补时/加时段余量%.2f≤%.1f，一击破盘",
                        mid, sport_l, played_mins, m_cfg["margin_full_mins"], margin, late_margin_floor,
                    )
                    return self._reject(
                        match_info, analysis,
                        f"{sport_l} 补时/加时under余量过薄（{margin:.2f}≤{late_margin_floor}）",
                    )
        if prediction == "under":
            logger.info(
                "[D1/under余量] ✅ 通过 match=%s | %s line=%s total=%d mins=%s margin=%s",
                mid, sport_l, total_line, current_total, played_mins,
                round(total_line - current_total, 2) if total_line is not None else None,
            )

        # ══════════════════════════════════════════════════════════
        # 阶段E：赔率有效性（区间检查 + 来源回显）
        # ══════════════════════════════════════════════════════════
        odds_raw_under = odds_data.get("under")
        odds_from_analysis = analysis.get("odds")
        try:
            odds = float(odds_data.get(prediction) or analysis.get("odds") or 0)
        except (TypeError, ValueError):
            odds = 0.0
        odds_source = "odds_data" if odds_data.get(prediction) else ("analysis" if odds_from_analysis else "无来源")
        logger.info(
            "[E1/赔率] match=%s | 取值=%.4f 来源=%s | "
            "odds_data={under:%s} analysis.odds=%s | "
            "区间[%.2f, %.2f]",
            mid, odds, odds_source,
            odds_raw_under, odds_from_analysis,
            min_odds_cfg, max_odds_cfg,
        )
        if odds <= 1.0:
            logger.info(
                "[E1/赔率] ❌ 拒绝 match=%s | 赔率=%.4f 无效(≤1.0) | 来源=%s | odds_data=%s analysis.odds=%s",
                mid, odds, odds_source, odds_data, odds_from_analysis,
            )
            return self._reject(match_info, analysis, f"赔率无效: {odds}")
        if odds < min_odds_cfg:
            logger.info(
                "[E1/赔率] ❌ 拒绝 match=%s | 赔率=%.4f < 最低=%.2f | 差距=%.4f | 来源=%s",
                mid, odds, min_odds_cfg, min_odds_cfg - odds, odds_source,
            )
            return self._reject(match_info, analysis, f"赔率 {odds} 低于最低 {min_odds_cfg}")
        if odds > max_odds_cfg:
            logger.info(
                "[E1/赔率] ❌ 拒绝 match=%s | 赔率=%.4f > 最高=%.2f | 超出=%.4f | 来源=%s",
                mid, odds, max_odds_cfg, odds - max_odds_cfg, odds_source,
            )
            return self._reject(match_info, analysis, f"赔率 {odds} 高于最高 {max_odds_cfg}")
        logger.info(
            "[E1/赔率] ✅ 通过 match=%s | 赔率=%.4f 在区间[%.2f, %.2f]内 | 来源=%s",
            mid, odds, min_odds_cfg, max_odds_cfg, odds_source,
        )

        # ── E2：小球 EV 盈亏平衡闸门 ──
        # 置信度必须覆盖赔率隐含的盈亏平衡概率（1/odds），否则长期必输。
        # 例：odds=1.65 需 conf≥0.606；odds=1.90 需 conf≥0.526；odds=2.50 需 conf≥0.40。
        # 修复前：conf=0.47 + odds=1.9 的 under 单 EV=-0.107 照常放行。
        ev_conf_edge = float(RISK.get("ev_conf_edge", 0.0) or 0.0)
        breakeven_conf = 1.0 / odds
        required_ev_conf = breakeven_conf + ev_conf_edge
        if conf_f < required_ev_conf:
            logger.info(
                "[E2/EV平衡] ❌ 拒绝 match=%s | conf=%.3f < 要求概率=%.3f (盈亏平衡=%.3f, edge=%.3f, odds=%.2f) | EV=%.3f",
                mid, conf_f, required_ev_conf, breakeven_conf, ev_conf_edge, odds, conf_f * odds - 1.0,
            )
            return self._reject(
                match_info, analysis,
                f"负EV单（conf {conf_f:.2f} 未覆盖要求 {required_ev_conf:.2f}，odds {odds:.2f}）",
            )
        logger.info(
            "[E2/EV平衡] ✅ 通过 match=%s | conf=%.2f ≥ 要求%.3f (盈亏平衡%.3f edge=%.3f odds=%.2f) | EV=%+.3f",
            mid, conf_f, required_ev_conf, breakeven_conf, ev_conf_edge, odds, conf_f * odds - 1.0,
        )

        # 投注金额：动态仓位 = 单笔上限 × 置信度缩放 × 风险折扣
        # - 置信度缩放：以 A3 实际门槛为起点（conf_lo=base_req），conf≥0.65 接近满仓（0.90 封顶）
        # - 小球方向加成 ×1.10（上限 0.95）
        # - 风险折扣：risk_score 0->不打折，1->七折（低置信/高赔率/多持仓自动降仓）
        max_amt = Decimal(str(self.config.max_bet_amount or 1))
        min_stake = Decimal(str(AI_MIN_STAKE))
        risk_score = self._calc_risk_score(confidence, odds, active_bets_count)

        conf_lo = max(min_conf, base_req)  # 以 A3 实际门槛为仓位起点，不用写死的 0.40
        if conf_f <= conf_lo:
            conf_scale = 0.5
        elif conf_f >= 0.65:
            conf_scale = 0.90  # 封顶 0.90：高置信不等于高胜率
        else:
            conf_scale = 0.5 + 0.4 * (conf_f - conf_lo) / (0.65 - conf_lo)
        conf_scale = min(conf_scale * 1.10, 0.95)
        risk_factor = 1.0 - min(max(risk_score, 0.0), 1.0) * 0.30
        suggested_stake = (
            max_amt * Decimal(str(round(conf_scale * risk_factor, 3)))
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        # ── 按站点分配仓位（provider-aware staking）──
        # 平博/OB 独立核算近期 ROI 与连败：亏损站自动降仓、回血后自动回升。
        # 小样本保护：结算 <8 单时不动（7 单 -30% 可能只是方差）。
        # 下限 0.3：降仓但不禁投，保留数据恢复通道。
        # 站点与小球细分统计均参与降仓，取两级因子的保守值。
        prov_factor = Decimal("1.0")
        prov_why = "n/a"

        def _bucket_factor(pb: dict, label: str) -> tuple[Decimal, str]:
            n_settled = int(pb.get("settled") or 0)
            if n_settled < 8:
                return Decimal("1.0"), f"{label}:样本不足(n={n_settled})"
            roi = pb.get("roi")
            streak = int(pb.get("loss_streak") or 0)
            f = 1.0
            if isinstance(roi, (int, float)):
                if roi <= -0.25:
                    f = 0.6
                elif roi <= -0.15:
                    f = 0.8
                elif roi >= 0.10:
                    f = 1.1
            if streak >= 6:
                f *= 0.35
            elif streak >= 4:
                f *= 0.5
            f = max(f, 0.3)
            roi_txt = f"{roi:+.0%}" if isinstance(roi, (int, float)) else "n/a"
            return Decimal(str(round(f, 3))), f"{label}:n={n_settled} roi={roi_txt} streak={streak}"

        try:
            prov_code = str(analysis.get("provider_code") or "").lower()
            if prov_code in ("pinnacle", "ob") and self._cached_stats:
                from app.services.bookmakers.catalog import provider_name

                prov_key = provider_name(prov_code)
                pb = (self._cached_stats.get("by_provider") or {}).get(prov_key) or {}
                f_site, why_site = _bucket_factor(pb, "站点")
                # 站点×小球方向细分
                sel_l = "under"
                psb = (pb.get("by_selection") or {}).get(sel_l) if sel_l else None
                f_dir, why_dir = (
                    _bucket_factor(psb, f"{sel_l}")
                    if isinstance(psb, dict)
                    else (Decimal("1.0"), f"{sel_l or '?'}:无细分")
                )
                # 保守值：两级中取较小（盈利方向不受站点级拖累）
                prov_factor = min(f_site, f_dir)
                prov_why = f"{why_site} | {why_dir}"
                if prov_factor != Decimal("1.0"):
                    suggested_stake = (
                        suggested_stake * prov_factor
                    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        except Exception as e:
            logger.debug("[仓位/站点] 统计加载失败(按1.0处理): %s", e)

        # 余额锚定：单笔仓位不超过可用余额的 25%（余额小时自动缩仓，
        # 避免推 90 元仓位但站点只剩 12 元、执行层静默丢弃）
        bal_f = float(user_balance or 0)
        if bal_f > 0:
            bal_cap = (Decimal(str(bal_f)) * Decimal("0.25")).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            suggested_stake = min(suggested_stake, bal_cap)
        # 日亏递减：当日已亏越多仓位越小（最多减半），连败自动收缩暴露
        loss_f = float(daily_loss or 0)
        stop_loss_f = float(getattr(self.config, "stop_loss", 0) or 0)
        if loss_f > 0 and stop_loss_f > 0:
            loss_ratio = min(loss_f / stop_loss_f, 1.0)
            taper = 1.0 - 0.5 * loss_ratio
            suggested_stake = (suggested_stake * Decimal(str(round(taper, 3)))).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )

        suggested_stake = min(suggested_stake, max_amt)
        # min_stake 兜底不击穿余额锚定：余额低于锚定值时用余额与 min_stake 的较小者
        if bal_cap > 0 and bal_cap < min_stake:
            suggested_stake = max(Decimal("0"), bal_cap)
        else:
            suggested_stake = max(suggested_stake, min_stake)

        logger.info(
            "[策略评估] ✅ match=%s 通过 | sel=%s conf=%.2f odds=%.2f | 下注=%.2f（conf_scale=%.2f risk=%.2f→×%.2f 站点×%.2f[%s]）max_bet=%.2f",
            mid, prediction, float(confidence or 0), odds,
            float(suggested_stake), conf_scale, risk_score, risk_factor,
            float(prov_factor), prov_why,
            float(self.config.max_bet_amount or 0),
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
        """综合风险评分（参与仓位计算，不影响放行决策）"""
        # 低置信度 -> 高风险
        risk = (1 - confidence) * settings.AI_RISK_LOW_CONF_WEIGHT

        # 高赔率 -> 高风险（under 实际赔率区间 1.5-1.95，阈值下调）
        if odds > 1.90:
            risk += settings.AI_RISK_HIGH_ODDS_PENALTY
        elif odds > 1.80:
            risk += settings.AI_RISK_MID_ODDS_PENALTY

        # 持仓多 -> 略增风险
        risk += min(active_count * settings.AI_RISK_ACTIVE_PENALTY, settings.AI_RISK_ACTIVE_CAP)

        return round(min(risk, 1.0), 2)
