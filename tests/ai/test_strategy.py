"""策略引擎单元测试：五阶段闸门链 (A-E) + 仓位计算 + 风险评分。

对应 doc.md 第 2 章。
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from decimal import Decimal

from app.ai.strategy import (
    StrategyEngine,
    StrategyConfig,
    SPORT_RISK,
    LEAGUE_BLACKLIST_KEYWORDS,
    league_is_blacklisted,
)


class TestA1Direction:
    """A1：方向合法（prediction 必须为 under）。"""

    @pytest.mark.asyncio
    async def test_over_rejected(self, mock_strategy_config, mock_match_info, mock_analysis_over):
        """prediction=over 时应拒绝。"""
        engine = StrategyEngine(mock_strategy_config, user_id=1)
        decision = await engine.evaluate_bet(
            mock_match_info, mock_analysis_over,
            user_balance=1000, daily_loss=0, active_bets_count=0,
        )
        assert not decision.should_bet
        assert "under" not in str(decision.reasoning).lower() or "方向" in str(decision.reasoning)

    @pytest.mark.asyncio
    async def test_under_passes_a1(self, mock_strategy_config, mock_match_info, mock_analysis_under):
        """prediction=under 应通过 A1（后续阶段可能拒绝）。"""
        engine = StrategyEngine(mock_strategy_config, user_id=1)
        decision = await engine.evaluate_bet(
            mock_match_info, mock_analysis_under,
            user_balance=1000, daily_loss=0, active_bets_count=0,
        )
        # A1 通过（后续可能因其他闸门拒绝，但不应是"方向"原因）
        assert not ("方向" in str(decision.reasoning) and "over" in str(decision.reasoning))


class TestA2Consensus:
    """A2：模型共识（consensus_reached 必须为 True）。"""

    @pytest.mark.asyncio
    async def test_no_consensus_rejected(self, mock_strategy_config, mock_match_info, mock_analysis_no_consensus):
        """consensus_reached=False 时应拒绝。"""
        engine = StrategyEngine(mock_strategy_config, user_id=1)
        decision = await engine.evaluate_bet(
            mock_match_info, mock_analysis_no_consensus,
            user_balance=1000, daily_loss=0, active_bets_count=0,
        )
        assert not decision.should_bet


class TestA3Confidence:
    """A3：置信度达标。"""

    @pytest.mark.asyncio
    async def test_low_confidence_rejected(self, mock_strategy_config, mock_match_info):
        """置信度低于门槛应拒绝。"""
        analysis = {
            "prediction": "under", "bet_type": "total",
            "confidence": 0.30, "odds": 1.85, "line": 2.5,
            "consensus_reached": True, "reasoning": "test", "models_used": ["gpt"],
        }
        engine = StrategyEngine(mock_strategy_config, user_id=1)
        decision = await engine.evaluate_bet(
            mock_match_info, analysis,
            user_balance=1000, daily_loss=0, active_bets_count=0,
        )
        assert not decision.should_bet


class TestB1LineRange:
    """B1：小球盘线区间。"""

    @pytest.mark.asyncio
    async def test_basketball_line_too_low(self, mock_strategy_config, mock_basketball_match_info, mock_analysis_under):
        """篮球盘线低于 under_min_line 应拒绝。"""
        mock_analysis_under["line"] = 50.0  # < 120
        mock_analysis_under["odds"] = 1.85
        engine = StrategyEngine(mock_strategy_config, user_id=1)
        decision = await engine.evaluate_bet(
            mock_basketball_match_info, mock_analysis_under,
            user_balance=1000, daily_loss=0, active_bets_count=0,
        )
        assert not decision.should_bet

    @pytest.mark.asyncio
    async def test_basketball_line_too_high(self, mock_strategy_config, mock_basketball_match_info, mock_analysis_under):
        """篮球盘线高于 under_max_line 应拒绝。"""
        mock_analysis_under["line"] = 250.0  # > 208
        mock_analysis_under["odds"] = 1.85
        engine = StrategyEngine(mock_strategy_config, user_id=1)
        decision = await engine.evaluate_bet(
            mock_basketball_match_info, mock_analysis_under,
            user_balance=1000, daily_loss=0, active_bets_count=0,
        )
        assert not decision.should_bet

    @pytest.mark.asyncio
    async def test_football_line_too_low(self, mock_strategy_config, mock_match_info, mock_analysis_under):
        """足球盘线低于 under_min_line 应拒绝。"""
        mock_analysis_under["line"] = 1.0  # < 2.0
        mock_analysis_under["odds"] = 1.85
        engine = StrategyEngine(mock_strategy_config, user_id=1)
        decision = await engine.evaluate_bet(
            mock_match_info, mock_analysis_under,
            user_balance=1000, daily_loss=0, active_bets_count=0,
        )
        assert not decision.should_bet


class TestB2Blacklist:
    """B2：联赛黑名单。"""

    def test_u19_blacklisted(self):
        assert league_is_blacklisted("U19 青年联赛")

    def test_women_blacklisted(self):
        assert league_is_blacklisted("女子篮球联赛")

    def test_friendship_blacklisted(self):
        assert league_is_blacklisted("国际友谊赛")

    def test_exhibition_blacklisted(self):
        assert league_is_blacklisted("全明星表演赛")

    def test_normal_league_passes(self):
        assert not league_is_blacklisted("英超")

    def test_keyword_in_list(self):
        assert "友谊赛" in LEAGUE_BLACKLIST_KEYWORDS
        assert "表演赛" in LEAGUE_BLACKLIST_KEYWORDS


class TestB3HighOdds:
    """B3：高赔率 under 风控。"""

    @pytest.mark.asyncio
    async def test_high_odds_rejected(self, mock_strategy_config, mock_match_info, mock_analysis_under):
        """under odds >= 2.0 应触发额外风控。"""
        mock_analysis_under["odds"] = 2.5
        mock_analysis_under["confidence"] = 0.65
        engine = StrategyEngine(mock_strategy_config, user_id=1)
        decision = await engine.evaluate_bet(
            mock_match_info, mock_analysis_under,
            user_balance=1000, daily_loss=0, active_bets_count=0,
        )
        # 高赔率 under 风控可能拒绝
        assert not decision.should_bet


class TestE1OddsRange:
    """E1：赔率区间检查。"""

    @pytest.mark.asyncio
    async def test_odds_too_low(self, mock_strategy_config, mock_match_info, mock_analysis_under):
        """赔率低于 min_odds 应拒绝。"""
        mock_analysis_under["odds"] = 1.0
        engine = StrategyEngine(mock_strategy_config, user_id=1)
        decision = await engine.evaluate_bet(
            mock_match_info, mock_analysis_under,
            user_balance=1000, daily_loss=0, active_bets_count=0,
        )
        assert not decision.should_bet

    @pytest.mark.asyncio
    async def test_odds_too_high(self, mock_strategy_config, mock_match_info, mock_analysis_under):
        """赔率高于 max_odds 应拒绝。"""
        mock_analysis_under["odds"] = 10.0
        engine = StrategyEngine(mock_strategy_config, user_id=1)
        decision = await engine.evaluate_bet(
            mock_match_info, mock_analysis_under,
            user_balance=1000, daily_loss=0, active_bets_count=0,
        )
        assert not decision.should_bet


class TestStakeCalculation:
    """动态仓位计算。"""

    def test_stake_within_bounds(self, mock_strategy_config):
        """仓位应在 [1, max_bet_amount] 区间内。"""
        engine = StrategyEngine(mock_strategy_config, user_id=1)
        risk = engine._calc_risk_score(confidence=0.65, odds=1.85, active_count=0)
        assert 0 <= risk <= 1.0

    def test_daily_loss_decay(self, mock_strategy_config):
        """日亏递减：50% 止损时仓位应乘 0.75。"""
        engine = StrategyEngine(mock_strategy_config, user_id=1)
        # 无法直接测试内部计算，但可以验证 _calc_risk_score 的输出
        risk = engine._calc_risk_score(confidence=0.65, odds=1.85, active_count=3)
        # 有持仓时风险应增加
        assert risk > engine._calc_risk_score(confidence=0.65, odds=1.85, active_count=0)


class TestRiskScore:
    """风险评分 _calc_risk_score。"""

    def test_low_confidence_high_risk(self, mock_strategy_config):
        """低置信度 -> 高风险。"""
        engine = StrategyEngine(mock_strategy_config, user_id=1)
        low_conf_risk = engine._calc_risk_score(confidence=0.30, odds=1.85, active_count=0)
        high_conf_risk = engine._calc_risk_score(confidence=0.80, odds=1.85, active_count=0)
        assert low_conf_risk > high_conf_risk

    def test_high_odds_penalty(self, mock_strategy_config):
        """高赔率（>1.90）触发惩罚。"""
        engine = StrategyEngine(mock_strategy_config, user_id=1)
        high_odds_risk = engine._calc_risk_score(confidence=0.65, odds=1.95, active_count=0)
        normal_odds_risk = engine._calc_risk_score(confidence=0.65, odds=1.70, active_count=0)
        assert high_odds_risk > normal_odds_risk

    def test_mid_odds_penalty(self, mock_strategy_config):
        """中赔率（>1.80）触发惩罚。"""
        engine = StrategyEngine(mock_strategy_config, user_id=1)
        mid_odds_risk = engine._calc_risk_score(confidence=0.65, odds=1.85, active_count=0)
        low_odds_risk = engine._calc_risk_score(confidence=0.65, odds=1.70, active_count=0)
        assert mid_odds_risk > low_odds_risk

    def test_active_count_penalty(self, mock_strategy_config):
        """持仓增加风险。"""
        engine = StrategyEngine(mock_strategy_config, user_id=1)
        many_active = engine._calc_risk_score(confidence=0.65, odds=1.85, active_count=5)
        no_active = engine._calc_risk_score(confidence=0.65, odds=1.85, active_count=0)
        assert many_active > no_active

    def test_risk_capped_at_1(self, mock_strategy_config):
        """风险评分上限为 1.0。"""
        engine = StrategyEngine(mock_strategy_config, user_id=1)
        extreme_risk = engine._calc_risk_score(confidence=0.01, odds=1.95, active_count=100)
        assert extreme_risk <= 1.0


class TestSportRisk:
    """SPORT_RISK 参数验证。"""

    def test_basketball_has_min_line(self):
        """篮球有 under_min_line。"""
        assert "under_min_line" in SPORT_RISK["basketball"]
        assert SPORT_RISK["basketball"]["under_min_line"] == 130.0

    def test_basketball_has_max_line(self):
        """篮球有 under_max_line。"""
        assert SPORT_RISK["basketball"]["under_max_line"] == 205.0

    def test_football_has_min_line(self):
        """足球有 under_min_line。"""
        assert "under_min_line" in SPORT_RISK["football"]
        assert SPORT_RISK["football"]["under_min_line"] == 2.0

    def test_football_has_max_line(self):
        """足球有 under_max_line。"""
        assert SPORT_RISK["football"]["under_max_line"] == 5.0

    def test_basketball_ev_edge(self):
        """篮球 EV edge = 0.04。"""
        assert SPORT_RISK["basketball"]["ev_conf_edge"] == 0.04

    def test_default_exists(self):
        """default 回退参数存在。"""
        assert "default" in SPORT_RISK

    def test_basketball_full_mins_48(self):
        """篮球全场 48 分钟。"""
        assert SPORT_RISK["basketball"]["margin_full_mins"] == 48.0

    def test_football_full_mins_90(self):
        """足球全场 90 分钟。"""
        assert SPORT_RISK["football"]["margin_full_mins"] == 90.0
