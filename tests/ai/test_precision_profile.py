from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from app.ai.analyzer import MatchAnalyzer
from app.ai.precision_profile import high_precision_history_eligible
from app.ai.strategy import (
    StrategyConfig,
    StrategyEngine,
    effective_strategy_from_ai_config,
)


def test_direction_confidence_cannot_bypass_final_calibration():
    analysis = {
        "prediction": "over",
        "confidence": 0.5273,
        "under_confidence": 0.0,
        "over_confidence": 0.72,
    }
    out = MatchAnalyzer._synchronize_direction_confidences(analysis)
    assert out["confidence"] == 0.5273
    assert out["over_confidence"] == 0.5273
    assert out["under_confidence"] == 0.0


def test_opposite_direction_is_capped_by_probability_complement():
    analysis = {
        "prediction": "under",
        "confidence": 0.74,
        "under_confidence": 0.80,
        "over_confidence": 0.40,
    }
    out = MatchAnalyzer._synchronize_direction_confidences(analysis)
    assert out["under_confidence"] == 0.74
    assert out["over_confidence"] == 0.26


def test_high_precision_history_profile_boundaries():
    base = dict(
        sport="football",
        selection="under",
        confidence=0.70,
        line=2.75,
        odds=1.85,
        played_minutes=55.0,
    )
    assert high_precision_history_eligible(**base)
    assert not high_precision_history_eligible(**{**base, "selection": "over"})
    assert not high_precision_history_eligible(**{**base, "confidence": 0.699})
    assert not high_precision_history_eligible(**{**base, "line": 2.0})
    assert not high_precision_history_eligible(**{**base, "line": 4.5})
    assert not high_precision_history_eligible(**{**base, "odds": 1.69})
    assert not high_precision_history_eligible(**{**base, "odds": 2.0})
    assert not high_precision_history_eligible(**{**base, "played_minutes": 44.9})
    assert not high_precision_history_eligible(**{**base, "played_minutes": 85.0})


def _production_engine() -> StrategyEngine:
    engine = StrategyEngine(
        StrategyConfig(
            name="simple",
            min_confidence=0.60,
            min_odds=1.65,
            max_odds=5.0,
            high_precision_mode=True,
        ),
        user_id=6,
    )
    engine._cached_stats = {
        "settled": 110,
        "by_sport": {
            "football": {"settled": 102, "win_rate": 0.588},
            "basketball": {"settled": 2, "win_rate": 0.0},
        },
        "by_selection": {
            "under": {"settled": 38, "win_rate": 0.711},
            "over": {"settled": 66, "win_rate": 0.50},
        },
        "by_provider": {},
    }
    return engine


def _evaluate(engine: StrategyEngine, *, selection: str, confidence: float):
    match = {
        "id": 9,
        "sport": "football",
        "league": "英超",
        "home_team": "A",
        "away_team": "B",
        "home_score": 0,
        "away_score": 0,
        "period": "2H",
        "clock": "55'",
        "total_line": 2.75,
        "odds": {"under": 1.85, "over": 1.85},
        "line_movements": {},
    }
    analysis = {
        "prediction": selection,
        "bet_type": "total",
        "confidence": confidence,
        "line": 2.75,
        "odds": 1.85,
        "context_source": "nowscore",
        "consensus_reached": True,
        "reasoning": "结构化证据完整",
    }
    with patch(
        "app.ai.calibration.load_risk_patterns", new=AsyncMock(return_value=[])
    ), patch(
        "app.ai.calibration.load_risk_tuning", new=AsyncMock(return_value={})
    ):
        return asyncio.run(
            engine.evaluate_bet(
                match_info=match,
                analysis=analysis,
                user_balance=Decimal("1000"),
                daily_loss=Decimal("0"),
                active_bets_count=0,
            )
        )


def test_production_precision_profile_allows_validated_under_window():
    decision = _evaluate(_production_engine(), selection="under", confidence=0.72)
    assert decision.should_bet is True


def test_production_precision_profile_pauses_losing_over_direction():
    decision = _evaluate(_production_engine(), selection="over", confidence=0.80)
    assert decision.should_bet is False
    assert "暂停over" in decision.reasoning


def test_default_production_strategy_enables_precision_mode():
    assert effective_strategy_from_ai_config(None).high_precision_mode is True
