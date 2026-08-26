from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.ai.analyzer import MatchAnalyzer
from app.ai.analysis_filters import sort_focused_leagues_first
from app.ai.balanced_profile import balanced_auto_eligible, balanced_min_confidence
from app.ai.league_focus import (
    basketball_regulation_minutes,
    league_focus_level,
)
from app.ai.precision_profile import high_precision_history_eligible
from app.ai.strategy import (
    StrategyConfig,
    StrategyEngine,
    effective_strategy_from_ai_config,
)
from app.services.bookmakers.match_live import match_elapsed_seconds


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
            balanced_target_mode=True,
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


def test_production_balanced_profile_allows_validated_under_window():
    decision = _evaluate(_production_engine(), selection="under", confidence=0.72)
    assert decision.should_bet is True


def test_focus_league_balanced_thresholds_cover_both_directions():
    assert balanced_min_confidence("football", "under", "英格兰超级联赛") == 0.70
    assert balanced_min_confidence("football", "under", "瑞典甲级联赛") == 0.70
    assert balanced_min_confidence("football", "over", "英格兰超级联赛") == 0.72
    assert balanced_min_confidence("basketball", "under", "NBA") == 0.72
    assert balanced_min_confidence("basketball", "over", "欧洲篮球联赛") == 0.72


def test_requested_leagues_are_prioritized_and_lower_tiers_are_not():
    for league in (
        "英格兰超级联赛", "西班牙甲级联赛", "意大利甲级联赛",
        "德国甲级联赛", "法国甲级联赛", "沙特职业联赛", "美职联",
        "巴西甲级联赛", "葡萄牙超级联赛", "荷兰甲级联赛",
        "阿根廷超级联赛", "墨西哥超级联赛", "欧洲冠军联赛",
    ):
        assert league_focus_level("football", league) == 2, league
    assert league_focus_level("football", "瑞典甲级联赛") == 1
    assert league_focus_level("football", "阿根廷职业联赛预备队") == 0
    assert league_focus_level("football", "美国 MLS NEXT 职业联赛") == 0


def test_focus_league_is_analyzed_before_non_focus_even_if_later_in_match():
    ordinary = SimpleNamespace(
        id=1, sport="football", league="芬兰丙级联赛",
        extra_data={"period": "1H", "clock": "5'"}, start_time=None,
    )
    focus = SimpleNamespace(
        id=2, sport="football", league="英格兰超级联赛",
        extra_data={"period": "1H", "clock": "35'"}, start_time=None,
    )
    assert [m.id for m in sort_focused_leagues_first([ordinary, focus])] == [2, 1]


def test_basketball_timing_distinguishes_nba_and_fiba_leagues():
    assert basketball_regulation_minutes("NBA") == 48.0
    assert basketball_regulation_minutes("西班牙ACB") == 40.0
    assert basketball_regulation_minutes("欧洲篮球联赛") == 40.0
    assert match_elapsed_seconds(
        sport="basketball", league="NBA", period="Q2", clock="6:00",
    ) == 18 * 60
    assert match_elapsed_seconds(
        sport="basketball", league="欧洲篮球联赛", period="Q2", clock="6:00",
    ) == 14 * 60


def test_balanced_profile_accepts_focus_basketball_both_sides():
    for selection in ("under", "over"):
        ok, _ = balanced_auto_eligible(
            sport="basketball",
            league="欧洲篮球联赛",
            selection=selection,
            confidence=0.73,
            line=164.5,
            odds=1.86,
            played_minutes=20.0,
        )
        assert ok is True


def test_balanced_profile_requires_exceptional_signal_for_non_focus_basketball():
    ok, why = balanced_auto_eligible(
        sport="basketball",
        league="IPBL篮球专业组",
        selection="over",
        confidence=0.77,
        line=164.5,
        odds=1.86,
        played_minutes=20.0,
    )
    assert ok is False
    assert "低于平衡档0.78" in why


def test_balanced_strategy_can_execute_nba_total_after_all_gates():
    engine = _production_engine()
    match = {
        "id": 10,
        "sport": "basketball",
        "league": "NBA",
        "home_team": "A",
        "away_team": "B",
        "home_score": 50,
        "away_score": 48,
        "period": "Q2",
        "clock": "6:00",
        "total_line": 220.0,
        "odds": {"under": 1.86, "over": 1.86},
        "line_movements": {},
    }
    analysis = {
        "prediction": "over",
        "bet_type": "total",
        "confidence": 0.73,
        "line": 220.0,
        "odds": 1.86,
        "context_source": "nowscore",
        "consensus_reached": True,
        "reasoning": "NBA盘口、节奏、基本面同向",
        "signal_review": {
            "triad_ready": True,
            "verdict": "supportive",
            "market_points": 5,
            "fundamental_points": 5,
            "conflict_points": 0,
        },
    }
    with patch(
        "app.ai.calibration.load_risk_patterns", new=AsyncMock(return_value=[])
    ), patch(
        "app.ai.calibration.load_risk_tuning", new=AsyncMock(return_value={})
    ):
        decision = asyncio.run(
            engine.evaluate_bet(
                match_info=match,
                analysis=analysis,
                user_balance=Decimal("1000"),
                daily_loss=Decimal("0"),
                active_bets_count=0,
            )
        )
    assert decision.should_bet is True, decision.reasoning


def test_default_production_strategy_enables_balanced_mode():
    strategy = effective_strategy_from_ai_config(None)
    assert strategy.balanced_target_mode is True
    assert strategy.high_precision_mode is False
