"""测试跨站同场去重：fixture_key 生成与同场判断。"""
import pytest
from datetime import datetime, timezone, timedelta

from app.services.fixture_key import fixture_key, same_fixture


class _MockMatch:
    """模拟 Match 对象"""
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
        assert "arsenal" in key.lower()
        assert "chelsea" in key.lower()

    def test_soccer_normalizes_to_football(self):
        key = fixture_key("soccer", "TeamA", "TeamB")
        assert key.startswith("football:")

    def test_home_away_order_independent(self):
        """主客对调应产生相同 key（按字典序排序）"""
        key1 = fixture_key("football", "Arsenal", "Chelsea")
        key2 = fixture_key("football", "Chelsea", "Arsenal")
        assert key1 == key2

    def test_different_teams_different_key(self):
        key1 = fixture_key("football", "Arsenal", "Chelsea")
        key2 = fixture_key("football", "Liverpool", "City")
        assert key1 != key2

    def test_different_sport_different_key(self):
        key1 = fixture_key("football", "TeamA", "TeamB")
        key2 = fixture_key("basketball", "TeamA", "TeamB")
        assert key1 != key2

    def test_start_time_bucket(self):
        """同场开赛时间微差应分到同一桶（6 小时窗口）"""
        t1 = datetime(2026, 8, 7, 14, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 8, 7, 15, 30, tzinfo=timezone.utc)
        key1 = fixture_key("football", "TeamA", "TeamB", start_time=t1)
        key2 = fixture_key("football", "TeamA", "TeamB", start_time=t2)
        assert key1 == key2

    def test_far_apart_times_different_bucket(self):
        t1 = datetime(2026, 8, 7, 14, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 8, 7, 21, 0, tzinfo=timezone.utc)  # 7h later -> different bucket
        key1 = fixture_key("football", "TeamA", "TeamB", start_time=t1)
        key2 = fixture_key("football", "TeamA", "TeamB", start_time=t2)
        assert key1 != key2


class TestSameFixture:
    def test_identical_teams(self):
        t = datetime(2026, 8, 7, 14, 0, tzinfo=timezone.utc)
        a = _MockMatch(1, "football", "Arsenal", "Chelsea", t)
        b = _MockMatch(2, "football", "Arsenal", "Chelsea", t)
        assert same_fixture(a, b)

    def test_swapped_home_away(self):
        """主客对调应视为同场"""
        t = datetime(2026, 8, 7, 14, 0, tzinfo=timezone.utc)
        a = _MockMatch(1, "football", "Arsenal", "Chelsea", t)
        b = _MockMatch(2, "football", "Chelsea", "Arsenal", t)
        assert same_fixture(a, b)

    def test_different_sport(self):
        t = datetime(2026, 8, 7, 14, 0, tzinfo=timezone.utc)
        a = _MockMatch(1, "football", "TeamA", "TeamB", t)
        b = _MockMatch(2, "basketball", "TeamA", "TeamB", t)
        assert not same_fixture(a, b)

    def test_different_teams(self):
        t = datetime(2026, 8, 7, 14, 0, tzinfo=timezone.utc)
        a = _MockMatch(1, "football", "Arsenal", "Chelsea", t)
        b = _MockMatch(2, "football", "Liverpool", "City", t)
        assert not same_fixture(a, b)

    def test_time_too_far(self):
        """开赛时间差 > 6h 不视为同场"""
        t1 = datetime(2026, 8, 7, 14, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 8, 7, 21, 0, tzinfo=timezone.utc)
        a = _MockMatch(1, "football", "Arsenal", "Chelsea", t1)
        b = _MockMatch(2, "football", "Arsenal", "Chelsea", t2)
        assert not same_fixture(a, b)

    def test_soccer_aliases_to_football(self):
        t = datetime(2026, 8, 7, 14, 0, tzinfo=timezone.utc)
        a = _MockMatch(1, "soccer", "TeamA", "TeamB", t)
        b = _MockMatch(2, "football", "TeamA", "TeamB", t)
        assert same_fixture(a, b)

    def test_no_start_time_still_matches(self):
        """无开赛时间时仅按球队名匹配"""
        a = _MockMatch(1, "football", "Arsenal", "Chelsea", None)
        b = _MockMatch(2, "football", "Arsenal", "Chelsea", None)
        assert same_fixture(a, b)
