"""测试中国赛事过滤。"""
import pytest

from app.services.bookmakers.china_match import is_china_match


class TestChinaMatch:
    def test_chinese_football_leagues(self):
        assert is_china_match(league="中超")
        assert is_china_match(league="中甲")
        assert is_china_match(league="中乙")
        assert is_china_match(league="足协杯")
        assert is_china_match(league="中国超级联赛")

    def test_english_china_leagues(self):
        assert is_china_match(league="Chinese Super League")
        assert is_china_match(league="CSL")
        assert is_china_match(league="China League One")

    def test_chinese_basketball(self):
        assert is_china_match(league="CBA")
        assert is_china_match(league="WCBA")
        assert is_china_match(league="中国男子篮球")

    def test_hong_kong_taiwan(self):
        assert is_china_match(league="港超")
        assert is_china_match(league="香港超级联赛")
        assert is_china_match(league="澳门联赛")
        assert is_china_match(league="台湾联赛")

    def test_chinese_team_names(self):
        """联赛名不含中国但队名是已知中国俱乐部"""
        assert is_china_match(league="Friendly", home="上海申花", away="北京国安")
        assert is_china_match(league="Unknown", home="山东泰山", away="广州队")

    def test_normal_matches(self):
        assert not is_china_match(league="Premier League", home="Arsenal", away="Chelsea")
        assert not is_china_match(league="La Liga", home="Real Madrid", away="Barcelona")
        assert not is_china_match(league="NBA", home="Lakers", away="Celtics")

    def test_empty_input(self):
        assert not is_china_match(league="", home="", away="")
        assert not is_china_match(league="", home="", away="", sport="")

    def test_sport_param_ignored(self):
        """sport 参数不影响过滤逻辑"""
        assert is_china_match(league="中超", sport="football")
        assert is_china_match(league="中超", sport="basketball")
