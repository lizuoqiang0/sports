from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.ai.total_features import build_total_feature_matrix
from app.ai.strategy import StrategyConfig, StrategyEngine
from app.services.nowscore_evidence import build_total_market_evidence


def _rows(totals):
    return [{"home_goals": total, "away_goals": 0} for total in totals]


def _context(sport="football", totals=None):
    totals = totals or [3, 4, 3, 2, 4]
    return {
        "source": "nowscore",
        "sport": sport,
        "identity": {"validated": True},
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "h2h": {"matches": _rows(totals[:3])},
        "home_form": {"matches": _rows(totals)},
        "away_form": {"matches": _rows(totals)},
        "quality": {
            "source": "nowscore", "completeness": 0.8,
            "identity_valid": True, "fresh": True,
        },
    }


def _with_evidence(ctx, line, sport):
    data = dict(ctx)
    data["total_market_evidence"] = build_total_market_evidence(
        data, line=line, sport=sport,
    )
    return data


def test_live_football_combines_asian_market_pace_and_nowscore():
    ctx = _with_evidence(_context(), 3.0, "football")
    match = {
        "sport": "football", "status": "live", "period": "1H", "clock": "30'",
        "home_score": 1, "away_score": 0,
    }
    market = {"markets": {"total": {
        "line": 3.0,
        "odds": {"over": 1.80, "under": 2.00},
        "opening": {"line": 2.5, "odds": {"over": 1.95, "under": 1.85}},
    }}}

    matrix = build_total_feature_matrix(
        match, market, ctx, line=3.0, line_source="bookmaker_market_total",
    )

    assert matrix["gates"]["analysis_ready"] is True
    assert matrix["match_state"]["phase"] == "first_half"
    assert matrix["asian_total_market"]["line_move_type"] == "market_up"
    assert matrix["asian_total_market"]["direction"] == "over"
    assert matrix["pace"]["played_minutes"] == 30.0
    assert matrix["nowscore"]["consensus"]["direction"] == "over"
    assert matrix["directional_summary"]["consensus_direction"] == "over"


def test_score_driven_line_increase_is_not_market_over_signal():
    ctx = _with_evidence(_context(totals=[2, 2, 2, 2, 2]), 3.5, "football")
    match = {
        "sport": "football", "status": "live", "period": "2H", "clock": "70'",
        "home_score": 3, "away_score": 0,
    }
    market = {"markets": {"total": {
        "line": 3.5,
        "odds": {"over": 1.90, "under": 1.90},
        "opening": {"line": 2.5, "odds": {"over": 1.90, "under": 1.90}},
    }}}

    matrix = build_total_feature_matrix(
        match, market, ctx, line=3.5, line_source="bookmaker_market_total",
    )

    assert matrix["asian_total_market"]["line_move_type"] == "score_adjustment"
    assert matrix["asian_total_market"]["line_direction"] == "neutral"


def test_live_match_without_parseable_clock_fails_closed():
    ctx = _with_evidence(_context(), 2.5, "football")
    match = {
        "sport": "football", "status": "live", "home_score": 0, "away_score": 0,
        "period": "", "clock": "",
    }
    market = {"markets": {"total": {
        "line": 2.5, "odds": {"over": 1.90, "under": 1.90},
    }}}

    matrix = build_total_feature_matrix(
        match, market, ctx, line=2.5, line_source="bookmaker_market_total",
    )

    assert matrix["gates"]["analysis_ready"] is False
    assert "missing_live_match_clock" in matrix["gates"]["hard_failures"]


def test_missing_two_sided_total_odds_fails_closed():
    ctx = _with_evidence(_context(), 2.5, "football")
    market = {"markets": {"total": {"line": 2.5, "odds": {"under": 1.90}}}}

    matrix = build_total_feature_matrix(
        {"sport": "football", "status": "upcoming", "home_score": 0, "away_score": 0},
        market, ctx, line=2.5, line_source="bookmaker_market_total",
    )

    assert matrix["match_state"]["is_live"] is False
    assert matrix["gates"]["analysis_ready"] is False
    assert "missing_two_sided_total_odds" in matrix["gates"]["hard_failures"]


def test_basketball_countdown_clock_is_converted_to_elapsed_time():
    ctx = _with_evidence(_context("basketball", [155, 160, 145, 170, 150]), 155.5, "basketball")
    match = {
        "sport": "basketball", "status": "live", "period": "Q4", "clock": "8:30",
        "home_score": 60, "away_score": 58,
    }
    market = {"markets": {"total": {
        "line": 155.5, "odds": {"over": 1.90, "under": 1.90},
    }}}

    matrix = build_total_feature_matrix(
        match, market, ctx, line=155.5, line_source="bookmaker_market_total",
    )

    assert matrix["match_state"]["phase"] == "q4"
    assert 39.0 <= matrix["match_state"]["played_minutes"] <= 40.0
    assert matrix["pace"]["available"] is True


@pytest.mark.asyncio
async def test_strategy_rejects_model_direction_against_feature_consensus():
    engine = StrategyEngine(StrategyConfig(min_confidence=0.0), user_id=1)
    matrix = {
        "gates": {"analysis_ready": True, "hard_failures": []},
        "directional_summary": {
            "consensus_direction": "under", "conflicts": [],
        },
    }
    analysis = {
        "prediction": "over", "bet_type": "total", "confidence": 0.9,
        "consensus_reached": True, "reasoning": "model says over",
        "total_feature_matrix": matrix,
    }

    decision = await engine.evaluate_bet(
        {"id": 1, "sport": "football", "odds": {"over": 1.9, "under": 1.9}},
        analysis, Decimal("1000"), Decimal("0"), 0,
    )

    assert decision.should_bet is False
    assert "结构化维度方向under" in decision.reasoning


@pytest.mark.asyncio
async def test_strategy_rejects_cross_dimension_conflict_before_confidence():
    engine = StrategyEngine(StrategyConfig(min_confidence=0.0), user_id=1)
    matrix = {
        "gates": {"analysis_ready": True, "hard_failures": []},
        "directional_summary": {
            "consensus_direction": "neutral",
            "conflicts": ["cross_dimension_direction_conflict"],
        },
    }
    analysis = {
        "prediction": "under", "bet_type": "total", "confidence": 0.99,
        "consensus_reached": True, "reasoning": "model says under",
        "total_feature_matrix": matrix,
    }

    decision = await engine.evaluate_bet(
        {"id": 2, "sport": "football", "odds": {"over": 1.9, "under": 1.9}},
        analysis, Decimal("1000"), Decimal("0"), 0,
    )

    assert decision.should_bet is False
    assert "结构化维度冲突" in decision.reasoning
