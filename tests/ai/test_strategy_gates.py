"""策略阀门单元测试：日风控 + 球队排除 + 球类偏好 + 仓位截断。

对应 doc.md 第 3 章。
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from decimal import Decimal

from app.ai.strategy import StrategyConfig
from app.ai.strategy_gates import (
    team_is_excluded,
    sport_is_preferred,
    cap_stake,
    stake_bounds,
    min_stake_floor,
    check_daily_risk,
    gate_recommendation_for_place,
)


class TestTeamExcluded:
    """球队排除检查。"""

    def test_team_in_excluded(self):
        """主队在排除列表中。"""
        assert team_is_excluded("中国队", "日本队", ["中国队", "韩国队"])

    def test_away_in_excluded(self):
        """客队在排除列表中。"""
        assert team_is_excluded("日本队", "中国队", ["中国队"])

    def test_team_not_excluded(self):
        """不在排除列表中。"""
        assert not team_is_excluded("Arsenal", "Chelsea", ["中国队"])

    def test_empty_excluded(self):
        """排除列表为空时不排除。"""
        assert not team_is_excluded("Arsenal", "Chelsea", None)
        assert not team_is_excluded("Arsenal", "Chelsea", [])


class TestSportPreferred:
    """球类偏好检查。"""

    def test_preferred_football(self):
        """足球在偏好列表中。"""
        assert sport_is_preferred("football", ["football", "basketball"])

    def test_preferred_basketball(self):
        """篮球在偏好列表中。"""
        assert sport_is_preferred("basketball", ["football", "basketball"])

    def test_not_preferred(self):
        """网球不在偏好列表中。"""
        assert not sport_is_preferred("tennis", ["football", "basketball"])

    def test_empty_preferred(self):
        """偏好为空=不限制。"""
        assert sport_is_preferred("football", None)
        assert sport_is_preferred("tennis", [])

    def test_soccer_normalized(self):
        """soccer 归一为 football。"""
        assert sport_is_preferred("soccer", ["football"])
        assert not sport_is_preferred("football", ["soccer"]) is False  # soccer->football


class TestCapStake:
    """仓位截断。"""

    def test_cap_high_stake(self, mock_strategy_config):
        """仓位超过上限截断。"""
        capped = cap_stake(Decimal("500"), mock_strategy_config)
        assert capped == Decimal("100")

    def test_cap_low_stake(self, mock_strategy_config):
        """仓位低于下限抬升。"""
        capped = cap_stake(Decimal("0.5"), mock_strategy_config)
        assert capped >= Decimal("1")

    def test_cap_normal_stake(self, mock_strategy_config):
        """正常仓位不变。"""
        capped = cap_stake(Decimal("50"), mock_strategy_config)
        assert capped == Decimal("50")

    def test_cap_zero_stake(self, mock_strategy_config):
        """零仓位回退最小注。"""
        capped = cap_stake(Decimal("0"), mock_strategy_config)
        assert capped >= Decimal("1")

    def test_stake_bounds(self, mock_strategy_config):
        """仓位区间。"""
        lo, hi = stake_bounds(mock_strategy_config)
        assert lo == Decimal("1")
        assert hi == Decimal("100")

    def test_min_stake_floor(self, mock_strategy_config):
        """最低仓位。"""
        floor = min_stake_floor(mock_strategy_config)
        assert floor == Decimal("1")


class TestCheckDailyRisk:
    """日风控检查。"""

    @pytest.mark.asyncio
    async def test_stop_loss_triggered(self, mock_strategy_config):
        """止损触发。"""
        with patch("app.ai.strategy_gates.calc_daily_pnl", new_callable=AsyncMock, return_value=Decimal("-600")):
            with patch("app.ai.strategy_gates.count_today_bets", new_callable=AsyncMock, return_value=3):
                triggered, reason = await check_daily_risk(MagicMock(), 1, mock_strategy_config)
        assert triggered
        assert "止损" in reason
        assert "600" in reason

    @pytest.mark.asyncio
    async def test_take_profit_triggered(self, mock_strategy_config):
        """止盈触发。"""
        with patch("app.ai.strategy_gates.calc_daily_pnl", new_callable=AsyncMock, return_value=Decimal("1200")):
            with patch("app.ai.strategy_gates.count_today_bets", new_callable=AsyncMock, return_value=3):
                triggered, reason = await check_daily_risk(MagicMock(), 1, mock_strategy_config)
        assert triggered
        assert "止盈" in reason

    @pytest.mark.asyncio
    async def test_max_bets_triggered(self, mock_strategy_config):
        """每日注数上限触发。"""
        with patch("app.ai.strategy_gates.calc_daily_pnl", new_callable=AsyncMock, return_value=Decimal("0")):
            with patch("app.ai.strategy_gates.count_today_bets", new_callable=AsyncMock, return_value=10):
                triggered, reason = await check_daily_risk(MagicMock(), 1, mock_strategy_config)
        assert triggered
        assert "上限" in reason

    @pytest.mark.asyncio
    async def test_no_risk_triggered(self, mock_strategy_config):
        """正常情况不触发。"""
        with patch("app.ai.strategy_gates.calc_daily_pnl", new_callable=AsyncMock, return_value=Decimal("100")):
            with patch("app.ai.strategy_gates.count_today_bets", new_callable=AsyncMock, return_value=3):
                triggered, reason = await check_daily_risk(MagicMock(), 1, mock_strategy_config)
        assert not triggered
        assert reason == ""


class TestGateRecommendationForPlace:
    """一键下单前完整策略校验。"""

    @pytest.mark.asyncio
    async def test_should_bet_false_rejected(self, mock_strategy_config):
        """推荐不可投注时拒绝。"""
        rec = {"recommendation": {"should_bet": False, "reasoning": "置信度不足"}}
        with patch("app.ai.strategy_gates.load_fresh_strategy", new_callable=AsyncMock, return_value=(None, mock_strategy_config)):
            ok, reason, stake, strat = await gate_recommendation_for_place(
                user_id=1, rec=rec, stake=Decimal("50"), db=MagicMock(),
            )
        assert not ok
        assert stake == Decimal("0")

    @pytest.mark.asyncio
    async def test_team_excluded_rejected(self, mock_strategy_config, mock_ai_config):
        """球队排除时拒绝。"""
        rec = {
            "recommendation": {"should_bet": True, "reasoning": "ok"},
            "home_team": "中国队",
            "away_team": "日本队",
            "sport": "football",
        }
        with patch("app.ai.strategy_gates.load_fresh_strategy", new_callable=AsyncMock, return_value=(mock_ai_config, mock_strategy_config)):
            ok, reason, stake, strat = await gate_recommendation_for_place(
                user_id=1, rec=rec, stake=Decimal("50"), db=MagicMock(),
            )
        assert not ok
        assert "排除" in reason

    @pytest.mark.asyncio
    async def test_sport_not_preferred_rejected(self, mock_strategy_config, mock_ai_config):
        """球类不在偏好列表中时拒绝。"""
        rec = {
            "recommendation": {"should_bet": True, "reasoning": "ok"},
            "home_team": "Federer",
            "away_team": "Nadal",
            "sport": "tennis",
        }
        with patch("app.ai.strategy_gates.load_fresh_strategy", new_callable=AsyncMock, return_value=(mock_ai_config, mock_strategy_config)):
            ok, reason, stake, strat = await gate_recommendation_for_place(
                user_id=1, rec=rec, stake=Decimal("50"), db=MagicMock(),
            )
        assert not ok
        assert "偏好" in reason or "球类" in reason

    @pytest.mark.asyncio
    async def test_daily_risk_triggered(self, mock_strategy_config, mock_ai_config):
        """日风控触发时拒绝。"""
        rec = {
            "recommendation": {"should_bet": True, "reasoning": "ok"},
            "home_team": "Arsenal",
            "away_team": "Chelsea",
            "sport": "football",
        }
        with patch("app.ai.strategy_gates.load_fresh_strategy", new_callable=AsyncMock, return_value=(mock_ai_config, mock_strategy_config)):
            with patch("app.ai.strategy_gates.check_daily_risk", new_callable=AsyncMock, return_value=(True, "止损触发")):
                ok, reason, stake, strat = await gate_recommendation_for_place(
                    user_id=1, rec=rec, stake=Decimal("50"), db=MagicMock(),
                )
        assert not ok
        assert "止损" in reason

    @pytest.mark.asyncio
    async def test_all_pass(self, mock_strategy_config, mock_ai_config):
        """全部通过。"""
        rec = {
            "recommendation": {"should_bet": True, "reasoning": "ok"},
            "home_team": "Arsenal",
            "away_team": "Chelsea",
            "sport": "football",
        }
        with patch("app.ai.strategy_gates.load_fresh_strategy", new_callable=AsyncMock, return_value=(mock_ai_config, mock_strategy_config)):
            with patch("app.ai.strategy_gates.check_daily_risk", new_callable=AsyncMock, return_value=(False, "")):
                ok, reason, stake, strat = await gate_recommendation_for_place(
                    user_id=1, rec=rec, stake=Decimal("50"), db=MagicMock(),
                )
        assert ok
        assert stake == Decimal("50")
