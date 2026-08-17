"""测试 AI 策略门禁：仓位边界、球队排除、球类偏好。"""
import pytest
from decimal import Decimal

from app.ai.strategy import StrategyConfig
from app.ai.strategy_gates import (
    stake_bounds,
    cap_stake,
    team_is_excluded,
    sport_is_preferred,
    min_stake_floor,
)


class TestStakeBounds:
    def test_normal(self):
        strat = StrategyConfig(max_bet_amount=100.0)
        lo, hi = stake_bounds(strat)
        assert lo == Decimal("1")
        assert hi == Decimal("100")


class TestCapStake:
    def test_within_bounds(self):
        strat = StrategyConfig(max_bet_amount=100.0)
        assert cap_stake(50, strat) == Decimal("50")

    def test_above_max(self):
        strat = StrategyConfig(max_bet_amount=100.0)
        assert cap_stake(200, strat) == Decimal("100")

    def test_below_min(self):
        strat = StrategyConfig(max_bet_amount=100.0)
        assert cap_stake(0.5, strat) == Decimal("1")

    def test_zero_defaults_to_minimum(self):
        strat = StrategyConfig(max_bet_amount=100.0)
        assert cap_stake(0, strat) == Decimal("1")


class TestMinStakeFloor:
    def test_with_strategy(self):
        strat = StrategyConfig(max_bet_amount=100.0)
        assert min_stake_floor(strat) == Decimal("1")

    def test_without_strategy(self):
        assert min_stake_floor(None) > 0


class TestTeamIsExcluded:
    def test_no_exclusion(self):
        assert not team_is_excluded("TeamA", "TeamB", None)
        assert not team_is_excluded("TeamA", "TeamB", [])

    def test_exact_match(self):
        assert team_is_excluded("TeamA", "TeamB", ["TeamA"])
        assert team_is_excluded("TeamA", "TeamB", ["TeamB"])

    def test_case_insensitive(self):
        assert team_is_excluded("teama", "teamb", ["TeamA"])

    def test_partial_match(self):
        assert team_is_excluded("Manchester United", "Liverpool", ["manchester"])

    def test_no_match(self):
        assert not team_is_excluded("TeamA", "TeamB", ["TeamC"])


class TestSportIsPreferred:
    def test_empty_allows_all(self):
        assert sport_is_preferred("football", [])
        assert sport_is_preferred("basketball", None)

    def test_match(self):
        assert sport_is_preferred("football", ["football"])
        assert sport_is_preferred("soccer", ["football"])
        assert sport_is_preferred("football", ["soccer"])

    def test_no_match(self):
        assert not sport_is_preferred("tennis", ["football"])
