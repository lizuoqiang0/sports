"""高精度自动投注档位的纯逻辑边界与历史评估口径。"""
from __future__ import annotations

from typing import Optional


TARGET_POINT_WIN_RATE = 0.80
MIN_BACKTEST_SAMPLE = 10

# 2026-08-21~24 的 110 笔已结算 AI 注单中，此区间为 11 中 9（81.8%）。
# 这是历史点估计，不是未来收益保证；生产仍需通过全部结构化闸门。
FOOTBALL_UNDER_PROFILE = {
    "min_confidence": 0.70,
    "min_line_exclusive": 2.0,
    "max_line_exclusive": 4.5,
    "min_odds": 1.70,
    "max_odds_exclusive": 2.0,
    "min_played_minutes": 45.0,
    "max_played_minutes_exclusive": 85.0,
}


def high_precision_history_eligible(
    *,
    sport: str,
    selection: str,
    confidence: float,
    line: Optional[float],
    odds: float,
    played_minutes: Optional[float],
) -> bool:
    """历史回测/审计统一口径；不替代 StrategyEngine 的完整闸门链。"""
    if str(sport or "").lower() != "football":
        return False
    if str(selection or "").lower() != "under":
        return False
    if line is None or played_minutes is None:
        return False
    p = FOOTBALL_UNDER_PROFILE
    return (
        float(confidence or 0.0) >= p["min_confidence"]
        and float(line) > p["min_line_exclusive"]
        and float(line) < p["max_line_exclusive"]
        and float(odds or 0.0) >= p["min_odds"]
        and float(odds or 0.0) < p["max_odds_exclusive"]
        and float(played_minutes) >= p["min_played_minutes"]
        and float(played_minutes) < p["max_played_minutes_exclusive"]
    )
