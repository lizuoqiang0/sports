"""测试跨站同场去重。"""
import pytest
from datetime import datetime, timezone

from app.services.fixture_key import fixture_key, same_fixture


class _MockMatch:
    __slots__ = ("id", "sport", "home_team", "away_team", "start_time")

    def __init__(self, mid, sport, home, away, start_time=None):
        self.id = mid
        self.sport = sport
        self.home_team = home
        self.away_team = away
        self.start_time = start_time


class TestFixtureKey:
    def test_basic_key(self):
        key = fixture_key("football", "Arsenal", "Chelsea")
        assert "football" in key

    def test_home_away_order_independent(self):
        key1 = fixture_key("football", "Arsenal", "Chelsea")
        key2 = fixture_key("football", "Chelsea", "Arsenal")
        assert key1 == key2

    def test_different_sport(self):
        key1 = fixture_key("football", "TeamA", "TeamB")
        key2 = fixture_key("basketball", "TeamA", "TeamB")
        assert key1 != key2

    def test_time_bucket(self):
        t1 = datetime(2026, 8, 7, 14, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 8, 7, 15, 30, tzinfo=timezone.utc)
        key1 = fixture_key("football", "TeamA", "TeamB", start_time=t1)
        key2 = fixture_key("football", "TeamA", "TeamB", start_time=t2)
        assert key1 == key2


class TestSameFixture:
    def test_identical(self):
        t = datetime(2026, 8, 7, 14, 0, tzinfo=timezone.utc)
        a = _MockMatch(1, "football", "Arsenal", "Chelsea", t)
        b = _MockMatch(2, "football", "Arsenal", "Chelsea", t)
        assert same_fixture(a, b)

    def test_swapped(self):
        t = datetime(2026, 8, 7, 14, 0, tzinfo=timezone.utc)
        a = _MockMatch(1, "football", "Arsenal", "Chelsea", t)
        b = _MockMatch(2, "football", "Chelsea", "Arsenal", t)
        assert same_fixture(a, b)

    def test_different_sport(self):
        t = datetime(2026, 8, 7, 14, 0, tzinfo=timezone.utc)
        a = _MockMatch(1, "football", "TeamA", "TeamB", t)
        b = _MockMatch(2, "basketball", "TeamA", "TeamB", t)
        assert not same_fixture(a, b)
