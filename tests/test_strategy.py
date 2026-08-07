"""测试 AI 投注策略引擎：Kelly 仓位、决策门禁、策略预设。"""
import pytest
from decimal import Decimal

from app.ai.strategy import (
    StrategyConfig,
    BetDecision,
    decision_passes_strategy,
    effective_strategy_from_ai_config,
    STRATEGIES,
)


class TestStrategyConfig:
    def test_default_is_balanced(self):
        cfg = StrategyConfig()
        assert cfg.name == "balanced"

    def test_presets_exist(self):
        assert "conservative" in STRATEGIES
        assert "balanced" in STRATEGIES
        assert "aggressive" in STRATEGIES

    def test_preset_risk_ordering(self):
        """conservative < balanced < aggressive 风险递增"""
        c = STRATEGIES["conservative"]
        b = STRATEGIES["balanced"]
        a = STRATEGIES["aggressive"]
        assert c.min_confidence > b.min_confidence > a.min_confidence
        assert c.max_bet_percentage < b.max_bet_percentage < a.max_bet_percentage
        assert c.kelly_fraction_cap < b.kelly_fraction_cap < a.kelly_fraction_cap


class TestDecisionPassesStrategy:
    def _make_decision(self, **kwargs):
        defaults = dict(
            match_id=1,
            selection="over",
            confidence=0.80,
            expected_value=0.05,
            kelly_fraction=0.15,
            suggested_stake=Decimal("50"),
            reasoning="test",
            risk_score=0.3,
            should_bet=True,
            bet_type="total",
            provider_code="ob",
            odds=1.90,
        )
        defaults.update(kwargs)
        return BetDecision(**defaults)

    def _make_strat(self, **kwargs):
        defaults = dict(
            min_confidence=0.75,
            min_odds=1.1,
            max_odds=10.0,
            max_bet_amount=100.0,
        )
        defaults.update(kwargs)
        return StrategyConfig(**defaults)

    def test_pass(self):
        d = self._make_decision()
        s = self._make_strat()
        ok, _ = decision_passes_strategy(d, s)
        assert ok

    def test_confidence_too_low(self):
        d = self._make_decision(confidence=0.50)
        s = self._make_strat(min_confidence=0.75)
        ok, why = decision_passes_strategy(d, s)
        assert not ok
        assert "置信度" in why

    def test_odds_too_low(self):
        d = self._make_decision(odds=1.05)
        s = self._make_strat(min_odds=1.1)
        ok, why = decision_passes_strategy(d, s)
        assert not ok
        assert "赔率" in why

    def test_odds_too_high(self):
        d = self._make_decision(odds=15.0)
        s = self._make_strat(max_odds=10.0)
        ok, why = decision_passes_strategy(d, s)
        assert not ok
        assert "赔率" in why

    def test_stake_too_high(self):
        d = self._make_decision(suggested_stake=Decimal("200"))
        s = self._make_strat(max_bet_amount=100.0)
        ok, why = decision_passes_strategy(d, s)
        assert not ok
        assert "仓位" in why

    def test_stake_too_low(self):
        d = self._make_decision(suggested_stake=Decimal("0.5"))
        s = self._make_strat()
        ok, why = decision_passes_strategy(d, s)
        assert not ok

    def test_should_bet_false(self):
        d = self._make_decision(should_bet=False)
        s = self._make_strat()
        ok, _ = decision_passes_strategy(d, s)
        assert not ok


class TestKellyStake:
    """测试 Kelly 仓位计算（修复后最低 10%）。"""

    @pytest.mark.asyncio
    async def test_min_stake_is_10_percent(self):
        """kelly=0 时应下 10% 而非 30%"""
        from app.ai.strategy import StrategyEngine

        cfg = StrategyConfig(
            max_bet_amount=100.0,
            min_confidence=0.50,
            min_odds=1.0,
            max_odds=10.0,
            kelly_fraction_cap=0.30,
        )
        engine = StrategyEngine(cfg)
        decision = await engine.evaluate_bet(
            match_info={"id": 1, "odds": {"over": 2.0}},
            analysis={
                "consensus_reached": True,
                "confidence": 0.55,
                "prediction": "over",
                "bet_type": "total",
                "expected_value": 0.10,
                "kelly_fraction": 0.0,  # Kelly = 0
                "odds": 2.0,
                "reasoning": "test",
            },
            user_balance=Decimal("1000"),
            daily_loss=Decimal("0"),
            active_bets_count=0,
        )
        assert decision.should_bet
        # kelly=0 -> ratio=0.10 -> stake=10% of max_bet_amount=100 -> 10
        assert float(decision.suggested_stake) == pytest.approx(10.0, abs=0.5)

    @pytest.mark.asyncio
    async def test_high_kelly_full_stake(self):
        """kelly=0.25 (cap for balanced) -> 100% 仓位"""
        from app.ai.strategy import StrategyEngine

        cfg = StrategyConfig(
            max_bet_amount=100.0,
            min_confidence=0.50,
            min_odds=1.0,
            max_odds=10.0,
            kelly_fraction_cap=0.30,
        )
        engine = StrategyEngine(cfg)
        decision = await engine.evaluate_bet(
            match_info={"id": 1, "odds": {"over": 2.0}},
            analysis={
                "consensus_reached": True,
                "confidence": 0.65,
                "prediction": "over",
                "bet_type": "total",
                "expected_value": 0.30,
                "kelly_fraction": 0.25,
                "odds": 2.0,
                "reasoning": "test",
            },
            user_balance=Decimal("1000"),
            daily_loss=Decimal("0"),
            active_bets_count=0,
        )
        assert decision.should_bet
        # kelly=0.25 -> 0.25*4=1.0 -> 100% of 100 -> 100
        assert float(decision.suggested_stake) == pytest.approx(100.0, abs=0.5)

    @pytest.mark.asyncio
    async def test_low_ev_rejected(self):
        """EV < 0.02 应被拒绝"""
        from app.ai.strategy import StrategyEngine

        cfg = StrategyConfig(
            max_bet_amount=100.0,
            min_confidence=0.50,
            min_odds=1.0,
            max_odds=10.0,
        )
        engine = StrategyEngine(cfg)
        decision = await engine.evaluate_bet(
            match_info={"id": 1, "odds": {"over": 1.05}},
            analysis={
                "consensus_reached": True,
                "confidence": 0.55,
                "prediction": "over",
                "bet_type": "total",
                "expected_value": 0.01,  # < 0.02
                "kelly_fraction": 0.0,
                "odds": 1.05,
                "reasoning": "test",
            },
            user_balance=Decimal("1000"),
            daily_loss=Decimal("0"),
            active_bets_count=0,
        )
        assert not decision.should_bet

    @pytest.mark.asyncio
    async def test_consensus_not_reached_rejected(self):
        from app.ai.strategy import StrategyEngine

        cfg = StrategyConfig(max_bet_amount=100.0)
        engine = StrategyEngine(cfg)
        decision = await engine.evaluate_bet(
            match_info={"id": 1, "odds": {"over": 2.0}},
            analysis={
                "consensus_reached": False,
                "confidence": 0.90,
                "prediction": "over",
                "bet_type": "total",
                "expected_value": 0.80,
                "kelly_fraction": 0.25,
                "odds": 2.0,
                "reasoning": "test",
            },
            user_balance=Decimal("1000"),
            daily_loss=Decimal("0"),
            active_bets_count=0,
        )
        assert not decision.should_bet


class TestEffectiveStrategyFromAiConfig:
    def test_none_returns_balanced(self):
        from types import SimpleNamespace

        cfg = effective_strategy_from_ai_config(None)
        assert cfg.name == "balanced"

    def test_user_overrides(self):
        from types import SimpleNamespace

        snap = SimpleNamespace(
            strategy="aggressive",
            max_bet_amount=50.0,
            max_daily_bets=5,
            min_confidence=0.80,
            preferred_sports=[],
            excluded_teams=[],
            stop_loss=200.0,
            take_profit=500.0,
            max_odds=5.0,
            min_odds=1.5,
            use_llm_analysis=True,
            auto_cashout=False,
            cashout_threshold=0.0,
            is_active=True,
        )
        cfg = effective_strategy_from_ai_config(snap)
        assert cfg.name == "aggressive"
        assert cfg.max_bet_amount == 50.0
        assert cfg.min_confidence == 0.80
        assert cfg.min_odds == 1.5
        assert cfg.max_odds == 5.0
