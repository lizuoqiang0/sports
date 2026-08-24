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
        # ── under 小球链（高赢单率配置）──
        "under_min_conf": 0.65,            # 0.58→0.65：与足球对齐，提高胜率
        "under_min_conf_no_fund": 0.68,    # 无基本面时加严
        "under_min_line": 130.0,           # 篮球小球盘线区间下限（低于此线不投）
        "under_max_line": 205.0,           # 208→205：高线小分容错极低
        "under_min_played_mins": 14.0,     # 首节/次节早段样本太小，不做小分
        "under_late_block_mins": 44.0,     # 末节最后 4 分钟犯规/罚球波动极大
        # under 余量（全场48分钟按盘口线等比折算剩余期望）
        "margin_min_mins": 14.0,           # 与 B1 早段衔接，14' 起即检查余量
        "margin_full_mins": 48.0,
        "margin_avg_goals": None,          # None=按盘口线折算
        "margin_factor": 1.20,             # 1.45→1.20：原值过严，半场时几乎全拒
        "late_margin_floor": 4.0,          # 末节/加时余量过薄时一波罚球就破盘
        "ev_conf_edge": 0.04,              # 小分必须明显高于盈亏平衡概率
        # ═══ 大球 over 组（独立闸门参数，与小球互不参与）═══
        "over_min_conf": 0.65,             # 0.58→0.65：与 under 对等
        "over_min_conf_no_fund": 0.68,     # 无基本面时加严
        "over_min_line": 140.0,            # 130→140：低线=市场极度看小，别硬刚
        "over_max_line": 165.0,            # 190→165：高线大分残余空间不足（194.5线/177球输单教训）
        "over_min_played_mins": 14.0,      # 早段样本小（独立于 under 配置）
        "over_late_block_mins": 40.0,      # 末节后段时间不够追分，经典送钱场景
        "over_pace_factor": 1.05,          # 1.20→1.05：线性 pace 低估后段得分，放宽
        "over_min_remaining_goals": 80.0,  # 55→80：半场 needed 普遍 80-90，原值全拒
    },
    "football": {
        # ── under 小球链（高赢单率配置）──
        "under_min_conf": 0.65,            # 0.58→0.65：实盘 5 单 3 负，低 conf 注单亏损严重
        "under_min_conf_no_fund": 0.68,    # 无基本面时进一步加严
        "under_min_line": 2.0,            # 2.5→2.0：2.5 是最常见盘线，0-0 时 margin=2.5 并非一球破盘
        "under_max_line": 5.0,             # 6.5→5.0：低级联赛高线（8.5）波动极大，8球破盘
        "under_min_played_mins": 20.0,
        "under_late_block_mins": 90.0,
        # under 余量（全场90分钟折算剩余期望）
        "margin_min_mins": 20.0,           # 40→20：与 B1 早段衔接，消除 20'-40' 保护盲区
        "margin_full_mins": 90.0,
        "margin_avg_goals": 2.55,          # 联赛均值偏高估，压低基准
        "margin_factor": 1.05,             # 线性折算高估前段，放宽中段
        "late_margin_floor": 0.5,
        "ev_conf_edge": 0.0,
        # ═══ 大球 over 组（独立闸门参数，与小球互不参与）═══
        "over_min_conf": 0.65,             # 0.58→0.65：与 under 对等
        "over_min_conf_no_fund": 0.68,     # 无基本面时加严
        "over_min_line": 2.5,              # 2.0→2.5：低线=市场极度看小
        "over_max_line": 4.5,              # 4.0→4.5：4.25 线在 2 球时仍可追，D1 速率闸门兜底无解场景
        "over_min_played_mins": 20.0,
        "over_late_block_mins": 85.0,      # 85'后追大球时间不够
        "over_pace_factor": 1.05,          # 与 under margin_factor 对称，不过度要求速率
        "over_min_remaining_goals": 3.5,   # 还差≥3.5球时基本无解；2.5球以内(如0:0追3球)仍可下单
    },
}
# 兜底：未知运动按足球参数处理（注意：深拷贝避免 default 与 football 同对象互改）
SPORT_RISK["default"] = {k: dict(v) if isinstance(v, dict) else v for k, v in SPORT_RISK["football"].items()}

# 联赛黑名单关键词（实盘教训：青少年/女子联赛进球极不稳定，2026-08-14 该类5注仅1胜）
# 同时用于：B2 下单闸门（strategy.evaluate_bet）+ 扫描层前置过滤（analysis_filters.skip_reason_for_match）
# 高波动联赛（冰岛/威尔士/澳洲NSW）实盘0%胜率，2026-08-22 纳入黑名单完全禁止
LEAGUE_BLACKLIST_KEYWORDS: tuple[str, ...] = (
    "u19", "u21", "u18", "u20", "u17", "u16",
    "青年", "青少年", "后备队", "女子", "女足", "(女)", "women", "女篮",
    "友谊赛", "表演赛",
    # 高波动联赛：进球数极不稳定，under/over实盘0%胜率
    "冰岛", "iceland",
    "新南威尔士", "nsw",
    "威尔士", "wales",
)


def league_is_blacklisted(league: str) -> bool:
    """联赛名是否命中黑名单（青少年/女子/高波动赛事进球不稳定）。"""
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

        # ── A0：玩法白名单（大小球 total 系，其他一律拒绝）──
        raw_bet_type = str(analysis.get("bet_type", "") or "").lower()
        if raw_bet_type and raw_bet_type not in ("total", "first_half_total", "second_half_total"):
            logger.info(
                "[A0/玩法] ❌ 拒绝 match=%s | bet_type='%s' 不在白名单(仅 total/first_half_total/second_half_total)",
                mid, raw_bet_type,
            )
            return self._reject(match_info, analysis, f"不支持的玩法: {raw_bet_type}（仅全场/半场大小球）")

        # ── A1：方向检查（under/over 均可参与闸门评估）──
        if prediction not in ("under", "over"):
            logger.info(
                "[A1/方向] ❌ 拒绝 match=%s | AI方向='%s' 不是大小球",
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

        # 有效置信度门槛（双闸门隔离）：under/over 各用各的地板参数，
        # 用户配置（>0时）仍全局生效（用户显式意愿优先于方向地板）。
        user_min_conf = min_conf if min_conf > 0 else 0.0
        if prediction == "over":
            floor_conf = float(RISK.get("over_min_conf", 0.62))
            floor_no_fund = float(RISK.get("over_min_conf_no_fund", 0.65))
        else:
            floor_conf = float(RISK["under_min_conf"])
            floor_no_fund = float(RISK["under_min_conf_no_fund"])
        if user_min_conf > 0:
            base_req = user_min_conf
        else:
            base_req = floor_conf  # 未配置时的兜底地板
        if not has_fundamentals:
            base_req = max(base_req, floor_no_fund)

        # 胜率自适应按方向隔离：under 链只看 under 结算、over 链只看 over 结算
        # （by_selection 由 recent_betting_stats 提供；样本<5 不加成不放宽，
        # over 上线初期即按硬地板运行）。
        adaptive_bump = 0.0
        try:
            if self._cached_stats is None:
                from app.services.bet_settlement import recent_betting_stats

                self._cached_stats = await recent_betting_stats(days=7, user_id=self.user_id)
            stats = self._cached_stats
            sel_stats = (stats.get("by_selection") or {}).get(prediction) or {}
            settled_n = int(sel_stats.get("settled") or 0)
            win_rate = sel_stats.get("win_rate")
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

        # ── 动态风控参数调优：基于近7天结算结果自动加严门槛 ──
        # （近7天某方向胜率<35% → conf_bump+0.08；<45% → +0.04）
        dyn_bump = 0.0
        try:
            from app.ai.calibration import get_dynamic_conf_bump, load_risk_tuning

            risk_tuning = await load_risk_tuning(user_id=self.user_id)
            dyn_bump = get_dynamic_conf_bump(sport_l, prediction, risk_tuning)
            if dyn_bump > 0:
                logger.info(
                    "[A3/动态调优] match=%s | %s %s 近7天低胜率 门槛+%.2f",
                    mid, sport_l, prediction, dyn_bump,
                )
        except Exception as e:
            logger.debug("[A3/动态调优] 跳过(异常): %s", e)

        # 时间维度：后半段 under 风险递增（进球集中在后段），提高置信度要求
        time_bump = 0.0
        if prediction == "under" and played_mins is not None:
            full_mins_a3 = float(RISK.get("margin_full_mins", 90.0))
            if played_mins >= full_mins_a3 * 0.65:  # 足球≥58'/篮球≥31'
                time_bump = 0.03
                logger.info(
                    "[A3/时间维度] match=%s | %s under 后半段(%.0f'≥%.0f'×65%%) 门槛+%.2f",
                    mid, sport_l, played_mins, full_mins_a3, time_bump,
                )

        # 加严叠加总上限：防止 adaptive + dynamic + time 叠加到不可通过的程度
        _MAX_TOTAL_BUMP = 0.12
        total_bump = adaptive_bump + dyn_bump + time_bump
        if total_bump > _MAX_TOTAL_BUMP:
            # 按比例缩减各 bump，保持相对权重
            scale = _MAX_TOTAL_BUMP / total_bump
            adaptive_bump = round(adaptive_bump * scale, 4)
            dyn_bump = round(dyn_bump * scale, 4)
            time_bump = round(time_bump * scale, 4)
            total_bump = _MAX_TOTAL_BUMP
            logger.info(
                "[A3/加严封顶] match=%s | 总加严>%.2f → 按比例缩减至 adaptive+%.2f dyn+%.2f time+%.2f",
                mid, _MAX_TOTAL_BUMP,
                adaptive_bump, dyn_bump, time_bump,
            )

        required_conf = base_req + total_bump
        if dyn_bump > 0 or time_bump > 0:
            logger.info(
                "[A3/门槛汇总] match=%s | base=%.2f + adaptive=%.2f + dyn=%.2f + time=%.2f → required=%.2f",
                mid, base_req, adaptive_bump, dyn_bump, time_bump, required_conf,
            )

        if conf_f < required_conf:
            # ── 校准后 EV 豁免：历史校准已将 conf 映射为实际胜率，
            # 如果实际胜率仍满足 EV 盈亏平衡（conf ≥ 1/odds + edge），
            # 说明该注单长期正 EV，不应被固定门槛误杀。
            is_calibrated = bool(analysis.get("calibration_note") or analysis.get("confidence_before_calibration"))
            if is_calibrated:
                try:
                    _ev_odds = float(odds_data.get(prediction) or analysis.get("odds") or 0)
                except (TypeError, ValueError):
                    _ev_odds = 0.0
                if _ev_odds > 1.0:
                    _ev_edge = float(RISK.get("ev_conf_edge", 0.0) or 0.0)
                    if prediction == "over":
                        _ev_edge = max(_ev_edge, 0.02)
                    _breakeven = 1.0 / _ev_odds + _ev_edge
                    if conf_f >= _breakeven:
                        logger.info(
                            "[A3/EV豁免] ✅ 校准后 conf=%.4f < 门槛%.4f 但 ≥ EV平衡%.4f (odds=%.2f) → 放行",
                            mid, conf_f, required_conf, _breakeven, _ev_odds,
                        )
                        # 跳过 A3 拒绝，继续后续闸门
                    else:
                        logger.info(
                            "[A3/置信度] ❌ 拒绝 match=%s | conf=%.4f < 要求=%.4f 且 < EV平衡%.4f (校准后仍不足)",
                            mid, conf_f, required_conf, _breakeven,
                        )
                        return self._reject(
                            match_info, analysis,
                            f"{prediction}置信度不足（校准后{conf_f:.2f}，要求{required_conf:.2f}，EV平衡{_breakeven:.2f}）",
                        )
                else:
                    logger.info(
                        "[A3/置信度] ❌ 拒绝 match=%s | conf=%.4f < 要求=%.4f (校准后无有效赔率)",
                        mid, conf_f, required_conf,
                    )
                    return self._reject(
                        match_info, analysis,
                        f"{prediction}置信度不足（当前{conf_f:.2f}，要求{required_conf:.2f}）",
                    )
            else:
                logger.info(
                    "[A3/置信度] ❌ 拒绝 match=%s | conf=%.4f < 要求=%.4f (用户配置=%.2f fundamentals=%s 自适应+%.2f)",
                    mid, conf_f, required_conf, user_min_conf, has_fundamentals, adaptive_bump,
                )
                return self._reject(
                    match_info, analysis,
                    f"{prediction}置信度不足（当前{conf_f:.2f}，要求{required_conf:.2f}）",
                )
        # ── P7/P9：过高置信度反向风险封顶（非拒绝）
        # 实盘数据显示高 conf 存在反向相关，但不直接丢弃信号，
        # 而是封顶到安全值，让闸门其余阶段继续评估。
        _REVERSE_CONF_CAP = 0.72
        if prediction == "under" and conf_f >= 0.74:
            logger.info(
                "[A3/P7] ⚠️ 封顶 match=%s | under conf=%.2f≥0.74 → 封顶%.2f（高conf反向风险）",
                mid, conf_f, _REVERSE_CONF_CAP,
            )
            conf_f = _REVERSE_CONF_CAP
            analysis["confidence"] = round(conf_f, 4)
            analysis["confidence_capped_reason"] = "under高置信度反向风险封顶0.72"
        elif prediction == "over" and conf_f >= 0.73:
            logger.info(
                "[A3/P9] ⚠️ 封顶 match=%s | over conf=%.2f≥0.73 → 封顶%.2f（高conf反向风险）",
                mid, conf_f, _REVERSE_CONF_CAP,
            )
            conf_f = _REVERSE_CONF_CAP
            analysis["confidence"] = round(conf_f, 4)
            analysis["confidence_capped_reason"] = "over高置信度反向风险封顶0.72"
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
        # A5：历史模式检测（基于近14天结算结果的多维度高风险模式拦截）
        #   维度：sport×selection×line_range / time_range / odds_range / conf_range
        #   命中 action=reject 的模式直接拒绝（近≥5注亏损率≥70%）
        # ══════════════════════════════════════════════════════════
        try:
            from app.ai.calibration import check_risk_patterns, load_risk_patterns

            patterns = await load_risk_patterns(user_id=self.user_id)
            if patterns:
                pattern_hit = check_risk_patterns(
                    sport=sport_l,
                    selection=prediction,
                    line=total_line,
                    played_mins=played_mins,
                    odds=float(odds_data.get(prediction) or analysis.get("odds") or 0),
                    confidence=conf_f,
                    patterns=patterns,
                )
                if pattern_hit:
                    logger.info(
                        "[A5/历史模式] ❌ 拒绝 match=%s | %s", mid, pattern_hit,
                    )
                    return self._reject(match_info, analysis, pattern_hit)
                logger.info(
                    "[A5/历史模式] ✅ 通过 match=%s | 已检查%d条高风险模式，未命中",
                    mid, len(patterns),
                )
        except Exception as e:
            logger.debug("[A5/历史模式] 跳过(异常): %s", e)

        # ══════════════════════════════════════════════════════════
        # 阶段B：结构性风控（盘口线本身的下注价值）
        #   B1 盘线区间 / 早段 / 末段 —— under 与 over 两套独立参数，互不参与
        # ══════════════════════════════════════════════════════════

        if prediction == "over":
            # ── B1-over 链（独立参数：over_min_played_mins/over_late_block_mins/
            #    over_min_line/over_max_line）──
            over_min_played = float(RISK.get("over_min_played_mins", 20.0))
            if played_mins is not None and played_mins < over_min_played:
                logger.info(
                    "[B1/over风控] ❌ 拒绝 match=%s | %s over早段不下 mins=%.1f<%.1f",
                    mid, sport_l, played_mins, over_min_played,
                )
                return self._reject(
                    match_info, analysis,
                    f"{sport_l} over前{over_min_played:.0f}分钟样本过小，保护性跳过",
                )
            over_late_block = float(RISK.get("over_late_block_mins", 85.0))
            if played_mins is not None and played_mins >= over_late_block:
                logger.info(
                    "[B1/over风控] ❌ 拒绝 match=%s | %s over末段不下 mins=%.1f≥%.1f 时间不够",
                    mid, sport_l, played_mins, over_late_block,
                )
                return self._reject(
                    match_info, analysis,
                    f"{sport_l} over末段（{played_mins:.0f}'≥{over_late_block:.0f}'）剩余时间不足以追大",
                )
            if total_line is not None:
                over_min_line = float(RISK.get("over_min_line", 2.0))
                over_max_line = float(RISK.get("over_max_line", 4.5))
                # 动态线距调整：基于近期亏损注单的盘口线分布
                try:
                    from app.ai.calibration import get_dynamic_line_adjustment, load_risk_tuning
                    _rt = await load_risk_tuning(user_id=self.user_id)
                    _adj = get_dynamic_line_adjustment(sport_l, "over", _rt)
                    if "min_line_bump" in _adj:
                        over_min_line += float(_adj["min_line_bump"])
                    if "max_line_shrink" in _adj:
                        over_max_line -= float(_adj["max_line_shrink"])
                except Exception:
                    pass
                if total_line <= over_min_line:
                    logger.info(
                        "[B1/over风控] ❌ 拒绝 match=%s | %s 低线over line=%.2f≤%.2f 市场极度看小",
                        mid, sport_l, total_line, over_min_line,
                    )
                    return self._reject(
                        match_info, analysis,
                        f"{sport_l} 低线over（line={total_line:.2f}）市场极度看小，风险过高",
                    )
                if total_line >= over_max_line:
                    logger.info(
                        "[B1/over风控] ❌ 拒绝 match=%s | %s 高线over line=%.2f>=%.2f 残余空间不足",
                        mid, sport_l, total_line, over_max_line,
                    )
                    return self._reject(
                        match_info, analysis,
                        f"{sport_l} 高线over（line={total_line:.2f}）残余进球空间不足",
                    )
                # ── P1：足球低进球高线over拒绝（0:0/1:0追3.25+线，实盘2注全输）──
                if sport_l == "football" and current_total <= 1 and total_line >= 3.25:
                    logger.info(
                        "[B1/over风控/P1] ❌ 拒绝 match=%s | 足球over低进球高线 total=%d line=%.2f 追球数过多",
                        mid, current_total, total_line,
                    )
                    return self._reject(
                        match_info, analysis,
                        f"足球over低进球高线（{current_total}球追{total_line:.2f}线），追球数过多风险极高",
                    )
                # ── P4：高置信+大差距over拒绝（conf≥0.70但当前总分远低于线，AI过度自信）──
                if conf_f >= 0.70 and current_total < total_line - 1.5:
                    logger.info(
                        "[B1/over风控/P4] ❌ 拒绝 match=%s | 高置信大差距over conf=%.2f total=%d line=%.2f gap=%.2f",
                        mid, conf_f, current_total, total_line, total_line - current_total,
                    )
                    return self._reject(
                        match_info, analysis,
                        f"高置信大差距over（conf={conf_f:.2f}，差{total_line - current_total:.1f}球），AI过度自信风险",
                    )
                # ── P10：半场线性外推陷阱（实盘over输单核心根因）──
                # 半场2球/45分钟 → 线性外推4球 → GPT给0.65 conf → 下半场0进球 → 输
                # 规则：足球 over 在半场附近(40-55')，当前进球≥2但 pace_projection 与 line 差距≤1球时
                #       线性外推高估后段产出，要求额外 0.05 置信度门槛
                if (sport_l == "football" and played_mins is not None
                        and 40.0 <= played_mins <= 55.0
                        and current_total >= 2
                        and total_line - current_total <= 1.5):
                    pace_proj = (current_total / played_mins) * 90.0
                    if pace_proj >= total_line and pace_proj - total_line <= 1.0:
                        logger.info(
                            "[B1/over风控/P10] ⚠️ 半场线性外推陷阱 match=%s | %.0f' %d球 pace_proj=%.1f line=%.2f 差%.1f",
                            mid, played_mins, current_total, pace_proj, total_line, pace_proj - total_line,
                        )
                        # 不直接拒绝，但在 A3 已通过的 conf 上额外加严 0.05
                        # （由 D1 衰减模型最终决定是否拦截）
                        if conf_f < 0.70:
                            logger.info(
                                "[B1/over风控/P10] ❌ 拒绝 match=%s | 半场外推陷阱 conf=%.2f<0.70 pace_proj仅高%.1f球",
                                mid, conf_f, pace_proj - total_line,
                            )
                            return self._reject(
                                match_info, analysis,
                                f"半场线性外推陷阱（{current_total}球@{played_mins:.0f}'外推{pace_proj:.1f}球，"
                                f"仅超线{pace_proj - total_line:.1f}球，下半场衰减风险高）",
                            )
        else:
            # ── B1-under 链（现状原样保留）──
            # 动态线距调整：基于近期亏损注单的盘口线分布
            _under_min_line_dyn = float(RISK.get("under_min_line", 1.5))
            _under_max_line_dyn = float(RISK.get("under_max_line", 5.0))
            try:
                from app.ai.calibration import get_dynamic_line_adjustment, load_risk_tuning
                _rt = await load_risk_tuning(user_id=self.user_id)
                _adj_u = get_dynamic_line_adjustment(sport_l, "under", _rt)
                if "min_line_bump" in _adj_u:
                    _under_min_line_dyn += float(_adj_u["min_line_bump"])
                if "max_line_shrink" in _adj_u:
                    _under_max_line_dyn -= float(_adj_u["max_line_shrink"])
            except Exception:
                pass
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
            if sport_l == "football" and total_line is not None and total_line <= _under_min_line_dyn:
                    logger.info("[B1/under风控] ❌ 拒绝 match=%s | 足球低线under line=%.2f，1球即破盘", mid, total_line)
                    return self._reject(match_info, analysis, f"足球低线under（line={total_line:.2f}）1球即破盘，风险过高")
            if sport_l == "football" and total_line is not None and total_line >= _under_max_line_dyn:
                    logger.info(
                        "[B1/under风控] ❌ 拒绝 match=%s | 足球高线under line=%.2f>=%.2f 容错极低",
                        mid, total_line, _under_max_line_dyn,
                    )
                    return self._reject(match_info, analysis, f"足球高线under（line={total_line:.2f}）残余进球空间不足")
            if sport_l == "football" and played_mins is not None and played_mins >= float(RISK.get("under_late_block_mins", 90.0)):
                    logger.info(
                        "[B1/under风控] ❌ 拒绝 match=%s | 足球under末段不下 mins=%.1f≥%.1f",
                        mid, played_mins, float(RISK.get("under_late_block_mins", 90.0)),
                    )
                    return self._reject(
                        match_info,
                        analysis,
                        f"足球under进入补时/加时段（{played_mins:.0f}'），保护性跳过",
                    )
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
            if sport_l == "basketball" and total_line is not None and total_line >= _under_max_line_dyn:
                    logger.info(
                        "[B1/under风控] ❌ 拒绝 match=%s | 篮球高线under line=%.1f>=%.1f，变数大",
                        mid, total_line, _under_max_line_dyn,
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

        # ── B1b：under 余量接近度保护（任何时段，余量过薄一击破盘）──
        if prediction == "under" and total_line is not None:
            margin = total_line - current_total
            if sport_l == "football" and margin <= 1.0:
                logger.info(
                    "[B1b/余量接近度] ❌ 拒绝 match=%s | 足球under余量%.2f≤1.0 一球即破盘",
                    mid, margin,
                )
                return self._reject(match_info, analysis, f"足球under余量过薄（{margin:.2f}球，一球即破盘）")
            if sport_l == "basketball" and margin <= 3.0:
                logger.info(
                    "[B1b/余量接近度] ❌ 拒绝 match=%s | 篮球under余量%.2f≤3.0 一波进攻即破盘",
                    mid, margin,
                )
                return self._reject(match_info, analysis, f"篮球under余量过薄（{margin:.2f}分，一波进攻即破盘）")

        # 联赛名（B2/P6 共用）
        league = str(match_info.get("league") or analysis.get("league") or "")

        # ── P5：under pace投影守卫（当前节奏推算全场进球≥盘口线时，不应押under）──
        #   实盘教训：#1 黑镇斯巴达 4球/54' pace投影6.7>5.8线，AI仍押under→终场8球惨输
        if prediction == "under" and total_line is not None and played_mins is not None and played_mins > 0:
            full_mins_p5 = float(RISK.get("margin_full_mins", 90.0))
            pace_projection = (current_total / played_mins) * full_mins_p5
            if pace_projection >= total_line:
                logger.info(
                    "[B1b/P5] ❌ 拒绝 match=%s | under pace投影%.2f≥线%.2f 节奏已超线不该押under",
                    mid, pace_projection, total_line,
                )
                return self._reject(
                    match_info, analysis,
                    f"under pace投影超标（当前节奏推算{pace_projection:.1f}球≥线{total_line:.1f}，不应押under）",
                )

        # ── P8：under 0-0高线陷阱（市场看大但暂未进球，后续可能爆发）──
        #   实盘教训：#45 卡莱尔 0-0@23' line=3.5 conf=0.60 → 终场5球惨输
        #   市场设高线(≥3.0)说明预期多进球，0-0只是暂未爆发，不该逆势押under
        if (prediction == "under" and total_line is not None
                and current_total == 0 and total_line >= 3.0
                and played_mins is not None and played_mins < 30):
            logger.info(
                "[B1b/P8] ❌ 拒绝 match=%s | under 0-0高线陷阱 0球@%.0f' line=%.2f≥3.0 市场看大不该逆势押under",
                mid, played_mins, total_line,
            )
            return self._reject(
                match_info, analysis,
                f"under 0-0高线陷阱（0球@{played_mins:.0f}' line={total_line:.1f}≥3.0，市场看大后续可能爆发）",
            )

        # ── B2：联赛质量闸门（关键词见模块级 LEAGUE_BLACKLIST_KEYWORDS，扫描层已前置过滤，此处兜底）──
        # 高波动联赛（冰岛/威尔士/澳洲NSW）已纳入黑名单，完全禁止下注
        if league_is_blacklisted(league):
            logger.info(
                "[B2/联赛质量] ❌ 拒绝 match=%s | 黑名单联赛 league=%s",
                mid, league,
            )
            return self._reject(
                match_info, analysis,
                f"联赛类型风控（{league}：青少年/女子/高波动赛事进球不稳定）",
            )

        # ── B3：高赔率风险（under/over 正常水位≤1.95，≥2.0 拒绝）──
        try:
            sel_odds_f = float(odds_data.get(prediction) or analysis.get("odds") or 0)
        except (TypeError, ValueError):
            sel_odds_f = 0.0
        if prediction == "over" and sel_odds_f >= 2.0:
            logger.info(
                "[B3/高赔率over] ❌ 拒绝 match=%s | over赔率=%.2f ≥2.0 市场强烈看小",
                mid, sel_odds_f,
            )
            return self._reject(
                match_info, analysis,
                f"大球赔率过高（{sel_odds_f:.2f}，不符合风险要求）",
            )
        if prediction == "under" and sel_odds_f >= 2.0:
            logger.info(
                "[B3/高赔率under] ❌ 拒绝 match=%s | under赔率=%.2f ≥2.0 市场强烈看大",
                mid, sel_odds_f,
            )
            return self._reject(
                match_info, analysis,
                f"小球赔率过高（{sel_odds_f:.2f}，不符合风险要求）",
            )

        # ── B3b：赔率-置信度一致性校验（高赔率=市场不确定性高，要求更强信号）──
        if sel_odds_f >= 1.90:
            odds_conf_penalty = 0.03
            if conf_f < required_conf + odds_conf_penalty:
                logger.info(
                    "[B3b/赔率一致性] ❌ 拒绝 match=%s | odds=%.2f≥1.90 要求conf≥%.2f+%.2f=%.2f 实际=%.2f",
                    mid, sel_odds_f, required_conf, odds_conf_penalty,
                    required_conf + odds_conf_penalty, conf_f,
                )
                return self._reject(
                    match_info, analysis,
                    f"高赔率({sel_odds_f:.2f})要求更高置信度({required_conf + odds_conf_penalty:.2f})，当前{conf_f:.2f}",
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
                        # 降盘：市场看小 → 支持 under
                        mkt_support = "under"
                        mkt_strength = "strong" if abs(ld) >= 0.5 else "medium"
                    elif ld >= 0.25:
                        # 升盘：市场看大 → 支持 over
                        mkt_support = "over"
                        mkt_strength = "strong" if abs(ld) >= 0.5 else "medium"
                except (TypeError, ValueError):
                    pass

        if mkt_support != "neutral" and mkt_support != prediction:
            logger.info(
                "[C1/盘口方向] ❌ 拒绝 match=%s | 市场支持%s 但AI预测%s | strength=%s",
                mid, mkt_support, prediction, mkt_strength,
            )
            return self._reject(match_info, analysis, f"盘口变化方向({mkt_support})与预测({prediction})相反")
        if sport_l == "basketball" and prediction == "over" and mkt_support == "under":
            logger.info(
                "[C1/盘口方向] ❌ 拒绝 match=%s | 篮球over但市场降盘看小 | strength=%s",
                mid, mkt_strength,
            )
            return self._reject(match_info, analysis, "篮球over与市场降盘方向冲突")
        if sport_l == "basketball" and prediction == "under" and mkt_support == "over":
            logger.info(
                "[C1/盘口方向] ❌ 拒绝 match=%s | 篮球under但市场升盘看大 | strength=%s",
                mid, mkt_strength,
            )
            return self._reject(match_info, analysis, "篮球under与市场升盘方向冲突")
        logger.info(
            "[C1/盘口方向] ✅ 通过 match=%s | 市场支持=%s 预测=%s strength=%s",
            mid, mkt_support, prediction, mkt_strength,
        )

        # ══════════════════════════════════════════════════════════
        # 阶段D：滚球余量/速率（双闸门：under 走余量，over 走速率，互不参与）
        #   under 实盘教训：bet58 下48' 1球押 under2.5，终场3球破线
        #   over 对称风险：末段追大差一脚，时间耗尽输单
        # ══════════════════════════════════════════════════════════

        if prediction == "over" and total_line is not None and played_mins is not None and played_mins > 0:
            # ── D1-over：进球速率闸门 ──
            # 所需进球 = line - 当前总分（over 需要剩余时间产出这些球）
            needed = max(0.0, total_line - current_total)
            over_min_remaining = float(RISK.get("over_min_remaining_goals", 2.0))
            if needed >= over_min_remaining:
                logger.info(
                    "[D1/over速率] ❌ 拒绝 match=%s | %s %.0f' 还需%.2f球≥%.2f 基本无解",
                    mid, sport_l, played_mins, needed, over_min_remaining,
                )
                return self._reject(
                    match_info, analysis,
                    f"{sport_l} over所需进球过多（还差{needed:.2f}，剩余时间无法保证）",
                )
            # 当前进球速率（球/分钟）× 剩余时间 = 预期产出
            # 使用时间分布加权模型替代纯线性外推（实盘教训：半场2球线性外推4球，下半场0进球）
            pace = current_total / played_mins
            full_mins = float(RISK.get("margin_full_mins", 90.0))
            remain_mins = max(0.0, full_mins - played_mins)
            sport_boost = 1.10 if sport_l == "basketball" else 1.05
            linear_expected = pace * remain_mins * sport_boost

            # ── 时间分布加权预期产出 ──
            # 足球进球时间权重：0-15'(12%) 15-30'(15%) 30-45'(18%) 45-60'(17%) 60-75'(22%) 75-90'(16%)
            # 篮球四节权重：Q1(22%) Q2(23%) Q3(25%) Q4(30%)
            if sport_l == "basketball":
                quarter_weights = [0.22, 0.23, 0.25, 0.30]
                quarter_mins = full_mins / 4
                completed_q = int(played_mins / quarter_mins)
                completed_w = sum(quarter_weights[:completed_q])
                intra_q = (played_mins % quarter_mins) / quarter_mins if completed_q < 4 else 1.0
                if completed_q < 4:
                    completed_w += quarter_weights[completed_q] * intra_q
                    remain_w = sum(quarter_weights[completed_q:]) - quarter_weights[completed_q] * intra_q
                else:
                    remain_w = 0.0
                if completed_w > 0 and remain_w > 0:
                    # 加权投影 = 当前进球 / 已完成权重 × 剩余权重
                    weighted_expected = (current_total / completed_w) * remain_w
                else:
                    weighted_expected = linear_expected
            else:
                seg_weights = [0.12, 0.15, 0.18, 0.17, 0.22, 0.16]
                seg_mins = 15.0
                completed_s = int(played_mins / seg_mins)
                completed_w = sum(seg_weights[:completed_s])
                intra_s = (played_mins % seg_mins) / seg_mins if completed_s < 6 else 1.0
                if completed_s < 6:
                    completed_w += seg_weights[completed_s] * intra_s
                    remain_w = sum(seg_weights[completed_s:]) - seg_weights[completed_s] * intra_s
                else:
                    remain_w = 0.0
                if completed_w > 0 and remain_w > 0:
                    weighted_expected = (current_total / completed_w) * remain_w
                else:
                    weighted_expected = linear_expected
                # 足球后段衰减：余量薄+后半段时进一步压缩预期
                margin_over = total_line - current_total
                if played_mins > 50 and margin_over <= 1.0 and remain_mins > 0:
                    late_factor = 0.7 if played_mins < 60 else (0.5 if played_mins < 75 else 0.3)
                    decayed_expected = pace * remain_mins * late_factor
                    # 取加权模型和衰减模型的较小值（更保守）
                    weighted_expected = min(weighted_expected, decayed_expected)

            # 使用加权预期产出（而非线性）做速率判定
            expected_output = weighted_expected
            pace_factor = float(RISK.get("over_pace_factor", 1.2))
            if expected_output < needed * pace_factor:
                logger.info(
                    "[D1/over速率] ❌ 拒绝 match=%s | %s %.0f' 预期产出%.2f < 所需%.2f×%.2f (pace=%.3f球/分)",
                    mid, sport_l, played_mins, expected_output, needed, pace_factor, pace,
                )
                return self._reject(
                    match_info, analysis,
                    f"{sport_l} over进球速率不足（预期{expected_output:.2f}，需{needed * pace_factor:.2f}）",
                )
            logger.info(
                "[D1/over速率] ✅ 通过 match=%s | %s line=%s total=%d mins=%s 需%.2f 预期%.2f pace=%.3f",
                mid, sport_l, total_line, current_total, played_mins, needed, expected_output, pace,
            )
        elif prediction == "under" and total_line is not None and played_mins is not None:
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
        # D1b：under 已进球接近度检查
        #   实盘教训：黑镇斯巴达 6-2 (8球) under 被打穿
        #   当已进球数接近盘口线（差 ≤ 1.5 球）时，1 次得分即破盘，
        #   提高 under 门槛 0.05，过滤高风险近线场景。
        # ══════════════════════════════════════════════════════════
        if prediction == "under" and total_line is not None:
            proximity_threshold = 1.5
            margin_to_line = total_line - current_total
            if margin_to_line <= proximity_threshold:
                proximity_bump = 0.05
                if conf_f < required_conf + proximity_bump:
                    logger.info(
                        "[D1b/under接近度] ❌ 拒绝 match=%s | %s 已进球%d 线%.2f 余量%.2f≤%.1f 门槛+%.2f→%.2f 实际%.2f",
                        mid, sport_l, current_total, total_line, margin_to_line,
                        proximity_threshold, proximity_bump, required_conf + proximity_bump, conf_f,
                    )
                    return self._reject(
                        match_info, analysis,
                        f"{sport_l} under近线风险（已{current_total}球，线{total_line:.2f}，余量仅{margin_to_line:.2f}球，一击破盘）",
                    )
                logger.info(
                    "[D1b/under接近度] ⚠️ 近线但置信度达标 match=%s | 已%d球 线%.2f 余量%.2f conf=%.2f≥%.2f",
                    mid, current_total, total_line, margin_to_line, conf_f, required_conf + proximity_bump,
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

        # ── E2：EV 盈亏平衡闸门（按方向区分 edge）──
        # 置信度必须覆盖赔率隐含的盈亏平衡概率（1/odds），否则长期必输。
        # over 方向不确定性更高，加 0.02 安全垫；under 保持运动特定 edge。
        ev_conf_edge = float(RISK.get("ev_conf_edge", 0.0) or 0.0)
        if prediction == "over":
            ev_conf_edge = max(ev_conf_edge, 0.02)
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
        # - 小球方向加成 ×1.10（上限 0.95）；over 观察期无加成（新方向先验证）
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
        if prediction == "under":
            conf_scale = min(conf_scale * 1.10, 0.95)
        else:
            # over 方向：高门槛（0.60）已过滤弱信号，折扣 0.6→0.8 提升仓位效率
            conf_scale = min(conf_scale, 0.95) * 0.8
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
                # 站点×方向细分：按实际投注方向查询统计，over 查 over、under 查 under
                sel_l = prediction
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
