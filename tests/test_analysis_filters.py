"""测试 AI 分析过滤：赔率区间、可用赔率提取。"""
import pytest

from app.ai.analysis_filters import (
    odds_in_configured_range,
    usable_total_odds,
    total_odds_meet_min,
    _as_eu_odds,
    DEFAULT_MIN_ODDS,
    DEFAULT_MAX_ODDS,
)


class TestAsEuOdds:
    def test_valid(self):
        assert _as_eu_odds(1.90) == 1.90
        assert _as_eu_odds("2.00") == 2.00

    def test_none_and_empty(self):
        assert _as_eu_odds(None) is None
        assert _as_eu_odds("") is None

    def test_too_low(self):
        assert _as_eu_odds(1.0) is None
        assert _as_eu_odds(1.01) is None

    def test_too_high(self):
        assert _as_eu_odds(500.1) is None

    def test_invalid(self):
        assert _as_eu_odds("abc") is None


class TestOddsInRange:
    def test_in_range(self):
        assert odds_in_configured_range(1.90, min_odds=1.1, max_odds=10.0)

    def test_below_min(self):
        assert not odds_in_configured_range(1.05, min_odds=1.1, max_odds=10.0)

    def test_above_max(self):
        assert not odds_in_configured_range(15.0, min_odds=1.1, max_odds=10.0)

    def test_no_max(self):
        assert odds_in_configured_range(100.0, min_odds=1.1, max_odds=None)

    def test_defaults(self):
        assert odds_in_configured_range(1.90)
        assert not odds_in_configured_range(1.0)


class TestUsableTotalOdds:
    def test_extract_over_under(self):
        result = usable_total_odds({"over": 1.90, "under": 1.95})
        assert 1.90 in result
        assert 1.95 in result
        assert len(result) == 2

    def test_ignores_other_keys(self):
        result = usable_total_odds({"over": 1.90, "home": 2.0, "under": 1.95})
        assert len(result) == 2  # only over/under

    def test_skips_invalid(self):
        result = usable_total_odds({"over": 0.5, "under": 1.95})
        assert len(result) == 1
        assert 1.95 in result

    def test_empty(self):
        assert usable_total_odds({}) == []
        assert usable_total_odds(None) == []


class TestTotalOddsMeetMin:
    def test_both_in_range(self):
        assert total_odds_meet_min({"over": 1.90, "under": 1.95}, floor=1.1, ceiling=10.0)

    def test_one_in_range(self):
        assert total_odds_meet_min({"over": 0.5, "under": 1.95}, floor=1.1, ceiling=10.0)

    def test_none_in_range(self):
        assert not total_odds_meet_min({"over": 0.5, "under": 0.6}, floor=1.1, ceiling=10.0)

    def test_empty(self):
        assert not total_odds_meet_min({}, floor=1.1, ceiling=10.0)
