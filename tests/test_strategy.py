"""测试简化后的 AI 投注策略引擎。"""
import pytest
from decimal import Decimal

from app.ai.strategy import (
    StrategyConfig,
    BetDecision,
    decision_passes_strategy,
    effective_strategy_from_ai_config,
    STRATEGIES,
    StrategyEngine,
)


class TestStrategyConfig:
    def test_default_is_simple(self):
        cfg = StrategyConfig()
        assert cfg.name == "simple"

    def test_only_simple_strategy(self):
        assert list(STRATEGIES.keys()) == ["simple"]

    def test_no_threshold_fields(self):
        cfg = StrategyConfig()
        assert not hasattr(cfg, "min_confidence")
        assert not hasattr(cfg, "min_odds")
        assert not hasattr(cfg, "max_odds")
        assert not hasattr(cfg, "allowed_bet_types")
        assert not hasattr(cfg, "max_bet_percentage")
        assert not hasattr(cfg, "kelly_fraction_cap")

    def test_has_basic_fields(self):
        cfg = StrategyConfig()
        assert hasattr(cfg, "max_bet_amount")
        assert hasattr(cfg, "max_daily_bets")
        assert hasattr(cfg, "stop_loss")
        assert hasattr(cfg, "take_profit")


class TestBetDecision:
    def test_bet_type_defaults_to_total(self):
        d = BetDecision(
            match_id=1, selection="over", confidence=0.8,
            suggested_stake=Decimal("50"), reasoning="test",
            risk_score=0.3, should_bet=True,
        )
        assert d.bet_type == "total"

    def test_no_signal_scores_field(self):
        d = BetDecision(
            match_id=1, selection="over", confidence=0.8,
            suggested_stake=Decimal("50"), reasoning="test",
            risk_score=0.3, should_bet=True,
        )
        assert not hasattr(d, "signal_scores")
        assert not hasattr(d, "llm_fallback")
        assert not hasattr(d, "context_quality")


class TestDecisionPassesStrategy:
    def _make_decision(self, **kwargs):
        defaults = dict(
            match_id=1, selection="over", confidence=0.80,
            suggested_stake=Decimal("50"), reasoning="test",
            risk_score=0.3, should_bet=True, bet_type="total",
            odds=1.90,
        )
        defaults.update(kwargs)
        return BetDecision(**defaults)

    def test_pass(self):
        d = self._make_decision()
        ok, _ = decision_passes_strategy(d, StrategyConfig())
        assert ok

    def test_should_bet_false(self):
        d = self._make_decision(should_bet=False)
        ok, _ = decision_passes_strategy(d, StrategyConfig())
        assert not ok

    def test_invalid_odds(self):
        d = self._make_decision(odds=0.5)
        ok, _ = decision_passes_strategy(d, StrategyConfig())
        assert not ok

    def test_stake_too_low(self):
        d = self._make_decision(suggested_stake=Decimal("0.5"))
        ok, _ = decision_passes_strategy(d, StrategyConfig())
        assert not ok


class TestStrategyEngine:
    @pytest.mark.asyncio
    async def test_over_passes(self):
        cfg = StrategyConfig(max_bet_amount=100.0)
        engine = StrategyEngine(cfg)
        decision = await engine.evaluate_bet(
            match_info={"id": 1, "odds": {"over": 1.90}},
            analysis={
                "consensus_reached": True,
                "confidence": 0.75,
                "prediction": "over",
                "bet_type": "total",
                "expected_value": 0.10,
                "odds": 1.90,
                "reasoning": "test",
            },
            user_balance=Decimal("1000"),
            daily_loss=Decimal("0"),
            active_bets_count=0,
        )
        assert decision.should_bet
        assert decision.bet_type == "total"
        assert decision.selection == "over"
        assert float(decision.suggested_stake) == pytest.approx(100.0, abs=0.5)

    @pytest.mark.asyncio
    async def test_consensus_not_reached_rejected(self):
        cfg = StrategyConfig(max_bet_amount=100.0)
        engine = StrategyEngine(cfg)
        decision = await engine.evaluate_bet(
            match_info={"id": 1, "odds": {"over": 1.90}},
            analysis={
                "consensus_reached": False,
                "confidence": 0.90,
                "prediction": "over",
                "bet_type": "total",
                "odds": 1.90,
                "reasoning": "test",
            },
            user_balance=Decimal("1000"),
            daily_loss=Decimal("0"),
            active_bets_count=0,
        )
        assert not decision.should_bet

    @pytest.mark.asyncio
    async def test_invalid_prediction_rejected(self):
        cfg = StrategyConfig(max_bet_amount=100.0)
        engine = StrategyEngine(cfg)
        decision = await engine.evaluate_bet(
            match_info={"id": 1, "odds": {"home": 1.90}},
            analysis={
                "consensus_reached": True,
                "confidence": 0.90,
                "prediction": "home",
                "bet_type": "total",
                "odds": 1.90,
                "reasoning": "test",
            },
            user_balance=Decimal("1000"),
            daily_loss=Decimal("0"),
            active_bets_count=0,
        )
        assert not decision.should_bet

    @pytest.mark.asyncio
    async def test_invalid_odds_rejected(self):
        cfg = StrategyConfig(max_bet_amount=100.0)
        engine = StrategyEngine(cfg)
        decision = await engine.evaluate_bet(
            match_info={"id": 1, "odds": {"over": 0.5}},
            analysis={
                "consensus_reached": True,
                "confidence": 0.90,
                "prediction": "over",
                "bet_type": "total",
                "odds": 0.5,
                "reasoning": "test",
            },
            user_balance=Decimal("1000"),
            daily_loss=Decimal("0"),
            active_bets_count=0,
        )
        assert not decision.should_bet


class TestEffectiveStrategy:
    def test_none_returns_simple(self):
        cfg = effective_strategy_from_ai_config(None)
        assert cfg.name == "simple"

    def test_user_overrides(self):
        from types import SimpleNamespace

        snap = SimpleNamespace(
            max_bet_amount=50.0,
            max_daily_bets=5,
            stop_loss=200.0,
            take_profit=500.0,
            use_llm_analysis=True,
            is_active=True,
        )
        cfg = effective_strategy_from_ai_config(snap)
        assert cfg.name == "simple"
        assert cfg.max_bet_amount == 50.0
        assert cfg.max_daily_bets == 5
