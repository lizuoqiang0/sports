"""置信度校准 + 高风险模式检测：基于历史投注结果闭环优化 AI 分析精度。

核心功能：
1. ConfidenceCalibrator — 按 0.05 宽度分桶，统计每桶实际胜率，将 DeepSeek 原始置信度
   映射到实际胜率，消除「高置信低胜率」的反向相关。
2. LossPatternAnalyzer — 多维度（运动×方向×线距×时段×赔率×置信度）检测历史
   亏损模式，返回高风险组合供策略闸门拦截。
3. DynamicRiskTuner — 基于近期结算结果动态调整 SPORT_RISK 参数（线距区间、
   时段门槛等），取代纯硬编码。

所有统计结果 Redis 缓存 5 分钟，避免逐场查 DB。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select

from app.core.cache import cache
from app.database import AsyncSessionLocal
from app.models.user import Bet, Match, BetStatus, MatchStatus

logger = logging.getLogger(__name__)

_CAL_CACHE_TTL = 300  # 5 分钟
_CAL_CACHE_KEY = "ai:calibration:v2"
_PATTERN_CACHE_KEY = "ai:patterns:v2"
_RISK_TUNING_CACHE_KEY = "ai:risk_tuning:v2"

# 置信度分桶宽度（0.05→0.10：每日注单量有限，宽桶增加每桶样本量，提升校准有效性）
_CONF_BUCKET_WIDTH = 0.10
# 每桶最低样本数：避免 4 注全胜就把 0.50 置信度抬到 0.65，造成过拟合。
# 需要至少 8 个已结算样本才允许改变模型置信度。
_MIN_BUCKET_SAMPLES = 8
# 模式检测：最低样本 + 最低亏损率（5→6 提高可靠性，0.60→0.65 降低误报）
_MIN_PATTERN_SAMPLES = 6
_PATTERN_LOSS_RATE_THRESHOLD = 0.65  # 亏损率 ≥65% 判定为高风险模式


def _conf_bucket(conf: float) -> str:
    """将置信度映射到分桶 key（如 0.65 → '0.65-0.70'）。"""
    lo = int(conf / _CONF_BUCKET_WIDTH) * _CONF_BUCKET_WIDTH
    return f"{lo:.2f}-{lo + _CONF_BUCKET_WIDTH:.2f}"


def _line_range(line: float, sport: str) -> str:
    """将盘口线映射到区间。"""
    if sport == "basketball":
        if line < 140:
            return "low(<140)"
        elif line < 160:
            return "mid(140-160)"
        elif line < 180:
            return "high(160-180)"
        elif line < 200:
            return "vhigh(180-200)"
        else:
            return "xhigh(≥200)"
    else:
        if line < 2.5:
            return "low(<2.5)"
        elif line < 3.0:
            return "mid(2.5-3.0)"
        elif line < 3.5:
            return "high(3.0-3.5)"
        elif line < 4.5:
            return "vhigh(3.5-4.5)"
        else:
            return "xhigh(≥4.5)"


def _time_range(played_mins: float, sport: str, league: str = "") -> str:
    """将已进行分钟映射到时段。"""
    if sport == "basketball":
        from app.ai.league_focus import basketball_regulation_minutes

        full = basketball_regulation_minutes(league)
    else:
        full = 90.0
    ratio = played_mins / full if full > 0 else 0
    if ratio < 0.25:
        return "early(0-25%)"
    elif ratio < 0.50:
        return "mid1(25-50%)"
    elif ratio < 0.75:
        return "mid2(50-75%)"
    else:
        return "late(75-100%)"


def _odds_range(odds: float) -> str:
    """将赔率映射到区间。"""
    if odds < 1.50:
        return "low(<1.50)"
    elif odds < 1.70:
        return "mid(1.50-1.70)"
    elif odds < 1.85:
        return "high(1.70-1.85)"
    elif odds < 2.00:
        return "vhigh(1.85-2.00)"
    else:
        return "xhigh(≥2.00)"


# ═══════════════════════════════════════════════════════════════
# 置信度校准器
# ═══════════════════════════════════════════════════════════════

async def load_calibration_table(user_id: Optional[int] = None) -> dict[str, dict]:
    """从 DB 加载置信度分桶 → 实际胜率映射。

    使用 30 天窗口（原14天样本量不足导致多数桶未达最低样本数）。

    返回结构:
    {
        "under": {
            "0.60-0.70": {"settled": 12, "won": 5, "win_rate": 0.417},
            "0.70-0.80": {"settled": 8, "won": 6, "win_rate": 0.750},
            ...
        },
        "over": { ... }
    }
    """
    cache_key = f"{_CAL_CACHE_KEY}:{user_id}" if user_id else _CAL_CACHE_KEY
    try:
        cached = await cache.get_json(cache_key)
        if isinstance(cached, dict) and cached.get("_loaded"):
            return cached
    except Exception:
        pass

    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30)
    conditions = [
        Bet.settled_at.is_not(None),
        Bet.created_at >= since,
        Bet.is_ai_bet.is_(True),
        Bet.status == BetStatus.SUCCESS,
    ]
    if user_id is not None:
        conditions.append(Bet.user_id == int(user_id))

    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(Bet, Match)
                .join(Match, Bet.match_id == Match.id)
                .where(*conditions)
                .where(Match.status == MatchStatus.FINISHED)
            )
        ).all()

    # 按 selection 分桶统计
    buckets: dict[str, dict[str, dict]] = {"under": {}, "over": {}}
    for bet, match in rows:
        sel = str(bet.selection or "").strip().lower()
        if sel not in ("under", "over"):
            continue
        conf = float(bet.ai_confidence or 0)
        if conf <= 0:
            continue
        payout = float(bet.actual_payout or 0)
        stake = float(bet.stake or 0)
        won = 1 if payout > stake + 1e-9 else (0 if payout < stake - 1e-9 else -1)  # -1=push
        if won == -1:
            continue  # 走水不计入

        bk = _conf_bucket(conf)
        b = buckets[sel].setdefault(bk, {"settled": 0, "won": 0})
        b["settled"] += 1
        b["won"] += won

    # 计算胜率
    for sel_data in buckets.values():
        for bk, b in sel_data.items():
            d = b["settled"]
            b["win_rate"] = round(b["won"] / d, 4) if d > 0 else None

    result = {**buckets, "_loaded": True, "_total_settled": len(rows)}
    try:
        await cache.set_json(cache_key, result, ttl=_CAL_CACHE_TTL)
    except Exception:
        pass
    return result


def calibrate_confidence(
    raw_conf: float,
    selection: str,
    calibration_table: dict,
) -> tuple[float, str]:
    """将 DeepSeek 原始置信度校准到实际胜率。

    策略:
    1. 找到 raw_conf 所在分桶
    2. 如果分桶样本 ≥ _MIN_BUCKET_SAMPLES，用实际胜率替代
    3. 样本不足时，使用相邻桶加权插值（若仍不足则返回原始值）

    返回 (calibrated_confidence, explanation)
    """
    sel = str(selection or "").strip().lower()
    sel_table = calibration_table.get(sel) or {}
    if not sel_table:
        return raw_conf, "无校准数据"

    bk = _conf_bucket(raw_conf)
    bucket = sel_table.get(bk)
    if bucket and bucket.get("settled", 0) >= _MIN_BUCKET_SAMPLES:
        actual_wr = bucket["win_rate"]
        if actual_wr is not None:
            delta = actual_wr - raw_conf
            # 限制单次校准幅度 ±0.15，避免极端样本导致跳变
            calibrated = max(0.0, min(1.0, raw_conf + max(-0.15, min(0.15, delta))))
            return round(calibrated, 4), (
                f"校准: {raw_conf:.2f}→{calibrated:.2f} "
                f"(桶{bk} 实际胜率{actual_wr:.1%}, n={bucket['settled']})"
            )

    # 尝试相邻桶插值
    lo = int(raw_conf / _CONF_BUCKET_WIDTH) * _CONF_BUCKET_WIDTH
    prev_key = f"{lo - _CONF_BUCKET_WIDTH:.2f}-{lo:.2f}"
    next_key = f"{lo + _CONF_BUCKET_WIDTH:.2f}-{lo + 2 * _CONF_BUCKET_WIDTH:.2f}"
    prev = sel_table.get(prev_key)
    next_b = sel_table.get(next_key)

    candidates = []
    if prev and prev.get("settled", 0) >= _MIN_BUCKET_SAMPLES and prev.get("win_rate") is not None:
        candidates.append(prev["win_rate"])
    if next_b and next_b.get("settled", 0) >= _MIN_BUCKET_SAMPLES and next_b.get("win_rate") is not None:
        candidates.append(next_b["win_rate"])

    if candidates:
        avg_wr = sum(candidates) / len(candidates)
        delta = avg_wr - raw_conf
        calibrated = max(0.0, min(1.0, raw_conf + max(-0.10, min(0.10, delta))))
        return round(calibrated, 4), (
            f"插值校准: {raw_conf:.2f}→{calibrated:.2f} "
            f"(相邻桶均值{avg_wr:.1%})"
        )

    return raw_conf, "样本不足，未校准"


# ═══════════════════════════════════════════════════════════════
# 高风险模式检测器
# ═══════════════════════════════════════════════════════════════

async def load_risk_patterns(user_id: Optional[int] = None) -> list[dict]:
    """从 DB 加载多维度高风险模式。

    使用 30 天窗口提升模式检测的统计显著性。

    返回结构:
    [
        {
            "dimension": "sport+selection+line_range",
            "sport": "football",
            "selection": "under",
            "line_range": "high(3.0-3.5)",
            "settled": 8, "won": 2, "loss_rate": 0.75,
            "action": "reject",
        },
        ...
    ]
    """
    cache_key = f"{_PATTERN_CACHE_KEY}:{user_id}" if user_id else _PATTERN_CACHE_KEY
    try:
        cached = await cache.get_json(cache_key)
        if isinstance(cached, list):
            return cached
    except Exception:
        pass

    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30)
    conditions = [
        Bet.settled_at.is_not(None),
        Bet.created_at >= since,
        Bet.is_ai_bet.is_(True),
        Bet.status == BetStatus.SUCCESS,
    ]
    if user_id is not None:
        conditions.append(Bet.user_id == int(user_id))

    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(Bet, Match)
                .join(Match, Bet.match_id == Match.id)
                .where(*conditions)
                .where(Match.status == MatchStatus.FINISHED)
            )
        ).all()

    # 多维度统计：key → {settled, won}
    dim_stats: dict[str, dict] = {}

    def _acc(key: str, won: int) -> None:
        d = dim_stats.setdefault(key, {"settled": 0, "won": 0, "meta": {}})
        d["settled"] += 1
        d["won"] += won

    for bet, match in rows:
        sel = str(bet.selection or "").strip().lower()
        if sel not in ("under", "over"):
            continue
        payout = float(bet.actual_payout or 0)
        stake = float(bet.stake or 0)
        won = 1 if payout > stake + 1e-9 else (0 if payout < stake - 1e-9 else -1)
        if won == -1:
            continue

        sport = str(match.sport.value if hasattr(match.sport, "value") else match.sport or "").lower()
        conf = float(bet.ai_confidence or 0)
        odds = float(bet.odds or 0)
        line = float(bet.line or 0)

        # 计算已进行分钟（从 match.extra_data 或 clock 推断）
        played_mins = 0.0
        extra = match.extra_data or {}
        if isinstance(extra, dict):
            clock = str(extra.get("clock") or "")
            period = str(extra.get("period") or "")
            try:
                from app.services.bookmakers.match_live import match_elapsed_seconds
                secs = match_elapsed_seconds(
                    sport=sport,
                    period=period,
                    clock=clock,
                    league=str(getattr(match, "league", "") or ""),
                )
                if secs is not None:
                    played_mins = secs / 60.0
            except Exception:
                pass

        # 维度1: sport + selection + line_range
        lr = _line_range(line, sport) if line > 0 else "unknown"
        k1 = f"{sport}|{sel}|line:{lr}"
        _acc(k1, won)
        dim_stats[k1]["meta"] = {"sport": sport, "selection": sel, "line_range": lr}

        # 维度2: sport + selection + time_range
        tr = _time_range(played_mins, sport, str(match.league or "")) if played_mins > 0 else "unknown"
        k2 = f"{sport}|{sel}|time:{tr}"
        _acc(k2, won)
        dim_stats[k2]["meta"] = {"sport": sport, "selection": sel, "time_range": tr}

        # 维度3: sport + selection + odds_range
        or_ = _odds_range(odds) if odds > 0 else "unknown"
        k3 = f"{sport}|{sel}|odds:{or_}"
        _acc(k3, won)
        dim_stats[k3]["meta"] = {"sport": sport, "selection": sel, "odds_range": or_}

        # 维度4: sport + selection + conf_range
        cr = _conf_bucket(conf) if conf > 0 else "unknown"
        k4 = f"{sport}|{sel}|conf:{cr}"
        _acc(k4, won)
        dim_stats[k4]["meta"] = {"sport": sport, "selection": sel, "conf_range": cr}

        # 维度5: sport + selection + line_range + time_range（交叉维度）
        k5 = f"{sport}|{sel}|line:{lr}|time:{tr}"
        _acc(k5, won)
        dim_stats[k5]["meta"] = {"sport": sport, "selection": sel, "line_range": lr, "time_range": tr}

    # 筛选高风险模式
    patterns: list[dict] = []
    for key, d in dim_stats.items():
        n = d["settled"]
        if n < _MIN_PATTERN_SAMPLES:
            continue
        loss_rate = 1.0 - (d["won"] / n)
        if loss_rate >= _PATTERN_LOSS_RATE_THRESHOLD:
            meta = d.get("meta", {})
            p = {
                "dimension": key,
                "settled": n,
                "won": d["won"],
                "loss_rate": round(loss_rate, 4),
                "win_rate": round(d["won"] / n, 4),
                # 阈值与 _PATTERN_LOSS_RATE_THRESHOLD 保持一致；原先这里写
                # 0.70，导致 15 注 5 胜（胜率33%）只生成 warn、闸门完全不拦。
                "action": "reject" if loss_rate >= _PATTERN_LOSS_RATE_THRESHOLD else "warn",
                **meta,
            }
            patterns.append(p)

    # 按样本数降序
    patterns.sort(key=lambda x: (-x["settled"], -x["loss_rate"]))

    try:
        await cache.set_json(cache_key, patterns, ttl=_CAL_CACHE_TTL)
    except Exception:
        pass
    return patterns


def check_risk_patterns(
    sport: str,
    selection: str,
    line: Optional[float],
    played_mins: Optional[float],
    odds: Optional[float],
    confidence: Optional[float],
    patterns: list[dict],
    league: str = "",
) -> Optional[str]:
    """检查当前比赛是否命中已知高风险模式。

    命中 action=reject 的模式时返回拒绝理由，否则返回 None。
    """
    if not patterns:
        return None

    sport_l = str(sport or "").lower()
    sel_l = str(selection or "").lower()
    line_val = float(line) if line is not None else 0.0
    mins_val = float(played_mins) if played_mins is not None else 0.0
    odds_val = float(odds) if odds is not None else 0.0
    conf_val = float(confidence) if confidence is not None else 0.0

    # 预计算当前比赛的各维度值
    cur_line_range = _line_range(line_val, sport_l) if line_val > 0 else None
    cur_time_range = _time_range(mins_val, sport_l, league) if mins_val > 0 else None
    cur_odds_range = _odds_range(odds_val) if odds_val > 0 else None
    cur_conf_range = _conf_bucket(conf_val) if conf_val > 0 else None

    for p in patterns:
        if p.get("action") != "reject":
            continue
        if p.get("sport") != sport_l or p.get("selection") != sel_l:
            continue

        # 逐维度匹配
        matched = True
        if p.get("line_range") and p["line_range"] != cur_line_range:
            matched = False
        if matched and p.get("time_range") and p["time_range"] != cur_time_range:
            matched = False
        if matched and p.get("odds_range") and p["odds_range"] != cur_odds_range:
            matched = False
        if matched and p.get("conf_range") and p["conf_range"] != cur_conf_range:
            matched = False

        if matched:
            return (
                f"历史高风险模式拦截: {sport_l} {sel_l} "
                f"line={cur_line_range} time={cur_time_range} "
                f"近{p['settled']}注仅赢{p['won']}注(胜率{p['win_rate']:.0%})"
            )

    return None


# ═══════════════════════════════════════════════════════════════
# 动态风控参数调优
# ═══════════════════════════════════════════════════════════════

async def load_risk_tuning(user_id: Optional[int] = None) -> dict:
    """基于近期结算结果，计算动态风控参数调整。

    返回结构:
    {
        "football": {
            "under": {
                "line_range_adjust": {"min_line_bump": 0.5, "max_line_shrink": 0.5},
                "conf_bump": 0.03,
                "sample_size": 15,
                "win_rate": 0.40,
            },
            "over": { ... }
        },
        "basketball": { ... }
    }
    """
    cache_key = f"{_RISK_TUNING_CACHE_KEY}:{user_id}" if user_id else _RISK_TUNING_CACHE_KEY
    try:
        cached = await cache.get_json(cache_key)
        if isinstance(cached, dict) and cached.get("_loaded"):
            return cached
    except Exception:
        pass

    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=7)
    conditions = [
        Bet.settled_at.is_not(None),
        Bet.created_at >= since,
        Bet.is_ai_bet.is_(True),
        Bet.status == BetStatus.SUCCESS,
    ]
    if user_id is not None:
        conditions.append(Bet.user_id == int(user_id))

    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(Bet, Match)
                .join(Match, Bet.match_id == Match.id)
                .where(*conditions)
                .where(Match.status == MatchStatus.FINISHED)
            )
        ).all()

    # 按 sport + selection 统计
    tuning: dict[str, dict[str, dict]] = {}
    for bet, match in rows:
        sel = str(bet.selection or "").strip().lower()
        if sel not in ("under", "over"):
            continue
        sport = str(match.sport.value if hasattr(match.sport, "value") else match.sport or "").lower()
        payout = float(bet.actual_payout or 0)
        stake = float(bet.stake or 0)
        won = 1 if payout > stake + 1e-9 else (0 if payout < stake - 1e-9 else -1)
        if won == -1:
            continue

        s_data = tuning.setdefault(sport, {}).setdefault(sel, {
            "settled": 0, "won": 0, "lines": [], "odds_list": [], "lost_lines": []
        })
        s_data["settled"] += 1
        s_data["won"] += won
        if bet.line:
            try:
                line_f = float(bet.line)
                s_data["lines"].append(line_f)
                if won == 0:
                    s_data["lost_lines"].append(line_f)
            except (TypeError, ValueError):
                pass
        if bet.odds:
            try:
                s_data["odds_list"].append(float(bet.odds))
            except (TypeError, ValueError):
                pass

    # 计算调整参数
    result: dict[str, dict[str, dict]] = {}
    for sport, sel_data in tuning.items():
        result[sport] = {}
        for sel, d in sel_data.items():
            n = d["settled"]
            if n < 5:
                result[sport][sel] = {"sample_size": n, "skip": True}
                continue
            wr = d["won"] / n
            r: dict[str, Any] = {
                "sample_size": n,
                "win_rate": round(wr, 4),
            }

            # 胜率 < 0.35：加严置信度门槛
            if wr < 0.35:
                r["conf_bump"] = 0.08
            elif wr < 0.45:
                r["conf_bump"] = 0.04
            else:
                r["conf_bump"] = 0.0

            # 分析亏损注单的盘口线分布
            lost_lines = d.get("lost_lines") or []
            if lost_lines:
                avg_lost_line = sum(lost_lines) / len(lost_lines)
                r["avg_lost_line"] = round(avg_lost_line, 2)
                # 如果亏损集中在特定线距，收紧区间
                if sport == "football":
                    if sel == "under" and avg_lost_line >= 3.5:
                        r["max_line_shrink"] = 0.5  # under_max_line 下调
                    elif sel == "over" and avg_lost_line <= 2.5:
                        r["min_line_bump"] = 0.25  # over_min_line 上调
                elif sport == "basketball":
                    if sel == "under" and avg_lost_line >= 190:
                        r["max_line_shrink"] = 5.0
                    elif sel == "over" and avg_lost_line <= 145:
                        r["min_line_bump"] = 5.0

            result[sport][sel] = r

    result["_loaded"] = True
    try:
        await cache.set_json(cache_key, result, ttl=_CAL_CACHE_TTL)
    except Exception:
        pass
    return result


def get_dynamic_conf_bump(
    sport: str,
    selection: str,
    risk_tuning: dict,
) -> float:
    """获取动态置信度加严值。"""
    sport_data = risk_tuning.get(sport) or {}
    sel_data = sport_data.get(selection) or {}
    if sel_data.get("skip"):
        return 0.0
    return float(sel_data.get("conf_bump") or 0.0)


def get_dynamic_line_adjustment(
    sport: str,
    selection: str,
    risk_tuning: dict,
) -> dict[str, float]:
    """获取动态盘口线区间调整。"""
    sport_data = risk_tuning.get(sport) or {}
    sel_data = sport_data.get(selection) or {}
    if sel_data.get("skip"):
        return {}
    return {
        k: v for k, v in sel_data.items()
        if k in ("max_line_shrink", "min_line_bump")
    }
