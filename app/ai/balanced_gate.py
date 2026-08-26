"""生产平衡档的单一组合闸门。

将历史上分散的 A/P/B/C/D/E 规则收敛为五个相互独立的检查：
数据一致性、最终概率、市场窗口、剩余得分概率、赔率EV。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Optional

from app.ai.balanced_profile import balanced_auto_eligible, balanced_min_confidence
from app.ai.league_focus import basketball_regulation_minutes


@dataclass(frozen=True)
class BalancedGateResult:
    allowed: bool
    reason: str
    required_confidence: float
    remaining_score_probability: Optional[float]
    gate: str


def _poisson_cdf(k: int, mean: float) -> float:
    if k < 0:
        return 0.0
    mean = max(0.0, min(float(mean), 100.0))
    term = math.exp(-mean)
    total = term
    for i in range(1, k + 1):
        term *= mean / i
        total += term
    return max(0.0, min(1.0, total))


def _football_remaining_goal_mean(
    *, played: float, current_total: int, line: float, feature_matrix: dict
) -> float:
    remain = max(0.0, 90.0 - played)
    pace = feature_matrix.get("pace") if isinstance(feature_matrix.get("pace"), dict) else {}
    projected = pace.get("adjusted_projection")
    try:
        model_remaining = max(0.0, float(projected) - current_total)
    except (TypeError, ValueError):
        model_remaining = 2.55 * remain / 90.0
    # 市场隐含的剩余进球与节奏投影各占一半；避免 D1 只用联赛均值导致几乎不拦。
    market_remaining = max(0.0, float(line) - current_total)
    blended = 0.50 * model_remaining + 0.50 * market_remaining
    return max(0.05, min(blended, 4.5 * remain / 90.0 + 0.25))


def _football_probability(
    selection: str,
    *,
    line: float,
    current_total: int,
    played: float,
    feature_matrix: dict,
) -> float:
    mean = _football_remaining_goal_mean(
        played=played, current_total=current_total, line=line, feature_matrix=feature_matrix,
    )
    remaining_line = float(line) - current_total
    if selection == "under":
        # 亚洲四分盘用保守整数边界：2.75 under在当前1球时，需要剩余≤1球。
        max_goals = math.ceil(remaining_line) - 1
        return _poisson_cdf(max_goals, mean)
    min_goals = math.floor(remaining_line) + 1
    return 1.0 - _poisson_cdf(min_goals - 1, mean)


def _basketball_probability(
    selection: str,
    *,
    line: float,
    current_total: int,
    played: float,
    full_minutes: float,
    feature_matrix: dict,
) -> float:
    pace = feature_matrix.get("pace") if isinstance(feature_matrix.get("pace"), dict) else {}
    projection = pace.get("adjusted_projection")
    try:
        projected_total = float(projection)
    except (TypeError, ValueError):
        projected_total = current_total / played * full_minutes if played > 0 else float(line)
    remain = max(0.0, full_minutes - played)
    # 得分差的标准差随剩余时间收缩，保留最小比赛波动。
    sigma = max(5.5, 14.0 * math.sqrt(remain / full_minutes))
    z = (float(line) - projected_total) / sigma
    under_p = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
    return under_p if selection == "under" else 1.0 - under_p


def evaluate_balanced_gate(
    *,
    match_info: dict[str, Any],
    analysis: dict[str, Any],
    selection: str,
    confidence: float,
    odds: float,
    line: Optional[float],
    played_minutes: Optional[float],
    configured_min_confidence: float = 0.0,
) -> BalancedGateResult:
    sport = str(match_info.get("sport") or "").lower()
    league = str(match_info.get("league") or "")
    required = max(
        balanced_min_confidence(sport, selection, league),
        max(0.0, float(configured_min_confidence or 0.0)),
    )
    matrix = analysis.get("total_feature_matrix") if isinstance(analysis.get("total_feature_matrix"), dict) else {}
    gates = matrix.get("gates") if isinstance(matrix.get("gates"), dict) else {}
    summary = matrix.get("directional_summary") if isinstance(matrix.get("directional_summary"), dict) else {}

    if not matrix or gates.get("analysis_ready") is not True:
        return BalancedGateResult(False, "结构化数据未就绪", required, None, "data")
    conflicts = [str(x) for x in (summary.get("conflicts") or []) if str(x)]
    direction = str(summary.get("consensus_direction") or "neutral").lower()
    if conflicts or direction != selection:
        detail = ",".join(conflicts) if conflicts else f"共识={direction}"
        return BalancedGateResult(False, f"盘口/节奏/基本面方向不一致: {detail}", required, None, "consensus")

    ok, why = balanced_auto_eligible(
        sport=sport,
        league=league,
        selection=selection,
        confidence=max(float(confidence), required),
        line=line,
        odds=odds,
        played_minutes=played_minutes,
    )
    if not ok:
        return BalancedGateResult(False, why, required, None, "profile")
    if float(confidence) < required:
        return BalancedGateResult(
            False,
            f"最终校准概率{float(confidence):.2f}低于组合门槛{required:.2f}",
            required,
            None,
            "confidence",
        )

    if line is None or played_minutes is None:
        return BalancedGateResult(False, "盘口线或比赛时间缺失", required, None, "market")
    current_total = int(float(match_info.get("home_score") or 0)) + int(float(match_info.get("away_score") or 0))
    if sport in ("football", "soccer"):
        remaining_prob = _football_probability(
            selection,
            line=float(line),
            current_total=current_total,
            played=float(played_minutes),
            feature_matrix=matrix,
        )
        probability_floor = 0.56
    else:
        remaining_prob = _basketball_probability(
            selection,
            line=float(line),
            current_total=current_total,
            played=float(played_minutes),
            full_minutes=basketball_regulation_minutes(league),
            feature_matrix=matrix,
        )
        probability_floor = 0.55
    if remaining_prob < probability_floor:
        return BalancedGateResult(
            False,
            f"剩余得分概率不足（{remaining_prob:.1%} < {probability_floor:.0%}）",
            required,
            remaining_prob,
            "remaining_probability",
        )

    ev_required = (1.0 / float(odds)) + (0.02 if selection == "over" else 0.01)
    if float(confidence) < ev_required:
        return BalancedGateResult(
            False,
            f"最终概率未覆盖赔率EV（{confidence:.1%} < {ev_required:.1%}）",
            required,
            remaining_prob,
            "ev",
        )
    return BalancedGateResult(True, "组合闸门通过", required, remaining_prob, "pass")
