from __future__ import annotations

from app.ai.balanced_gate import evaluate_balanced_gate
from app.ai.strategy import StrategyEngine


def _matrix(direction: str, projection: float) -> dict:
    return {
        "gates": {"analysis_ready": True, "hard_failures": []},
        "directional_summary": {"consensus_direction": direction, "conflicts": []},
        "pace": {"adjusted_projection": projection},
    }


def test_football_under_remaining_probability_rejects_fast_game():
    result = evaluate_balanced_gate(
        match_info={
            "sport": "football", "league": "英格兰超级联赛",
            "home_score": 2, "away_score": 1,
        },
        analysis={"total_feature_matrix": _matrix("under", 5.5)},
        selection="under", confidence=0.72, odds=1.85, line=4.25,
        played_minutes=50.0,
    )
    assert result.allowed is False
    assert result.gate == "remaining_probability"


def test_football_under_remaining_probability_allows_real_margin():
    result = evaluate_balanced_gate(
        match_info={
            "sport": "football", "league": "英格兰超级联赛",
            "home_score": 0, "away_score": 0,
        },
        analysis={"total_feature_matrix": _matrix("under", 1.8)},
        selection="under", confidence=0.72, odds=1.85, line=2.75,
        played_minutes=55.0,
    )
    assert result.allowed is True, result.reason
    assert result.remaining_score_probability is not None
    assert result.remaining_score_probability >= 0.56


def test_consensus_conflict_rejects_before_probability_math():
    matrix = _matrix("over", 3.4)
    matrix["directional_summary"]["conflicts"] = ["asian_line_vs_price"]
    result = evaluate_balanced_gate(
        match_info={
            "sport": "football", "league": "英格兰超级联赛",
            "home_score": 1, "away_score": 1,
        },
        analysis={"total_feature_matrix": matrix},
        selection="over", confidence=0.75, odds=1.85, line=2.75,
        played_minutes=45.0,
    )
    assert result.allowed is False
    assert result.gate == "consensus"


def test_configured_minimum_can_only_raise_combined_threshold():
    result = evaluate_balanced_gate(
        match_info={
            "sport": "football", "league": "英格兰超级联赛",
            "home_score": 0, "away_score": 0,
        },
        analysis={"total_feature_matrix": _matrix("under", 1.8)},
        selection="under", confidence=0.72, odds=1.85, line=2.75,
        played_minutes=55.0, configured_min_confidence=0.75,
    )
    assert result.allowed is False
    assert result.required_confidence == 0.75
    assert result.gate == "confidence"


def test_production_entry_bypasses_legacy_rule_chain():
    """公共入口在平衡档下必须直接走组合闸门，旧链不可叠加执行。"""
    import inspect

    public_source = inspect.getsource(StrategyEngine.evaluate_bet)
    balanced_source = inspect.getsource(StrategyEngine._evaluate_balanced_bet)
    assert "_evaluate_balanced_bet" in public_source
    assert "_evaluate_legacy_bet" in public_source
    assert "recent_betting_stats" not in balanced_source
    assert "load_risk_tuning" not in balanced_source
    assert "check_risk_patterns" not in balanced_source
    assert "margin_factor" not in balanced_source
