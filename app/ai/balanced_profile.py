"""足球/篮球按联赛等级和 under/over 方向区分的自动投注门槛。"""
from __future__ import annotations

from typing import Optional

from app.ai.league_focus import basketball_regulation_minutes, league_focus_level


FOCUS_UNDER_MIN = 0.58
FOCUS_OVER_MIN = 0.68
OTHER_UNDER_MIN = 0.62
OTHER_OVER_MIN = 0.78


def balanced_min_confidence(sport: str, selection: str, league: str) -> float:
    sport_l = str(sport or "").lower()
    selection_l = str(selection or "").lower()
    focus = league_focus_level(sport_l, league)
    if sport_l in ("football", "soccer"):
        if selection_l == "under":
            return FOCUS_UNDER_MIN if focus >= 1 else OTHER_UNDER_MIN
        if selection_l == "over":
            return FOCUS_OVER_MIN if focus >= 1 else OTHER_OVER_MIN
    if sport_l == "basketball":
        if selection_l == "under":
            return FOCUS_UNDER_MIN if focus >= 2 else OTHER_UNDER_MIN
        if selection_l == "over":
            return FOCUS_OVER_MIN if focus >= 2 else OTHER_OVER_MIN
    return 1.0


def balanced_auto_eligible(
    *,
    sport: str,
    league: str,
    selection: str,
    confidence: float,
    line: Optional[float],
    odds: float,
    played_minutes: Optional[float],
) -> tuple[bool, str]:
    sport_l = str(sport or "").lower()
    selection_l = str(selection or "").lower()
    if line is None or played_minutes is None:
        return False, "缺少盘口线或比赛时间"
    required = balanced_min_confidence(sport_l, selection_l, league)
    if float(confidence or 0.0) < required:
        return False, f"最终校准概率{float(confidence or 0):.2f}低于平衡档{required:.2f}"
    if not (1.70 <= float(odds or 0.0) < 2.0):
        return False, "赔率不在平衡档[1.70,2.00)"

    line_f = float(line)
    mins_f = float(played_minutes)
    if sport_l in ("football", "soccer"):
        if selection_l == "under":
            if not (2.0 < line_f < 4.5 and 30.0 <= mins_f < 85.0):
                return False, "足球under不在盘口(2.0,4.5)/时间[30,85)窗口"
        elif selection_l == "over":
            if not (2.25 < line_f < 3.5 and 25.0 <= mins_f < 75.0):
                return False, "足球over不在盘口(2.25,3.5)/时间[25,75)窗口"
        else:
            return False, "方向不是全场大小球"
        return True, f"football focus={league_focus_level(sport_l, league)} required={required:.2f}"

    if sport_l == "basketball":
        full = basketball_regulation_minutes(league)
        progress = mins_f / full if full > 0 else 0.0
        # NBA常见高盘；ACB/欧篮联及其他FIBA赛事使用较低盘线。
        if full >= 48.0:
            line_ok = 185.0 < line_f < 260.0
        else:
            line_ok = 120.0 < line_f < 200.0
        if not line_ok:
            return False, f"篮球盘口{line_f:.1f}不在该联赛合理区间"
        if not (0.25 <= progress < 0.875):
            return False, "篮球仅在常规时间25%–87.5%进度窗口自动下注"
        return True, f"basketball focus=2 required={required:.2f} full_minutes={full:.0f}"

    return False, "不支持的运动类型"
