"""测试中国赛事过滤。"""
import pytest

from app.services.bookmakers.china_match import is_china_match


class TestChinaMatch:
    def test_chinese_football_leagues(self):
        assert is_china_match(league="中超")
        assert is_china_match(league="中甲")
        assert is_china_match(league="足协杯")

    def test_english_china_leagues(self):
        assert is_china_match(league="Chinese Super League")
        assert is_china_match(league="CSL")

    def test_chinese_basketball(self):
        assert is_china_match(league="CBA")
        assert is_china_match(league="WCBA")

    def test_normal_matches(self):
        assert not is_china_match(league="Premier League", home="Arsenal", away="Chelsea")
        assert not is_china_match(league="La Liga", home="Real Madrid", away="Barcelona")
        assert not is_china_match(league="NBA", home="Lakers", away="Celtics")

    def test_empty(self):
        assert not is_china_match(league="", home="", away="")
