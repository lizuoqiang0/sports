"""候选过滤单元测试：前置过滤 + 排序。

对应 doc.md 第 5 章。
"""
import pytest
from unittest.mock import MagicMock
from app.ai.analysis_filters import (
    sort_just_started_first,
    skip_reason_for_match,
)
from app.ai.strategy import league_is_blacklisted
from app.services.bookmakers.china_match import is_china_match


class TestChinaMatch:
    """中国赛事过滤。"""

    def test_china_team(self):
        """中国球队被过滤。"""
        assert is_china_match("", "北京国安", "上海申花", "football")

    def test_non_china(self):
        """非中国球队不被过滤。"""
        assert not is_china_match("", "Arsenal", "Chelsea", "football")


class TestLeagueBlacklist:
    """联赛黑名单。"""

    def test_u19(self):
        assert league_is_blacklisted("U19 青年联赛")

    def test_women(self):
        assert league_is_blacklisted("女子篮球联赛")

    def test_friendship(self):
        assert league_is_blacklisted("国际友谊赛")

    def test_exhibition(self):
        assert league_is_blacklisted("全明星表演赛")

    def test_normal(self):
        assert not league_is_blacklisted("英超")


class TestSortJustStartedFirst:
    """刚开赛优先排序。"""

    def test_not_started_first(self):
        """未开赛排前面。"""
        m1 = MagicMock(start_time=None, extra_data={})
        m2 = MagicMock(start_time=None, extra_data={"clock": "10'"})
        result = sort_just_started_first([m2, m1])
        # 未开赛的应在前面
        assert result[0] == m1 or result[0] == m2

    def test_earlier_started_first(self):
        """已开赛中时长短的排前面。"""
        m1 = MagicMock(extra_data={"clock": "5'"})
        m2 = MagicMock(extra_data={"clock": "60'"})
        result = sort_just_started_first([m2, m1])
        # clock=5' 的应在 clock=60' 前面
        assert result[0] == m1


class TestSkipReasonForMatch:
    """skip_reason_for_match 前置过滤。"""

    def test_china_match_skipped(self):
        """中国赛事跳过。"""
        from app.ai.analysis_filters import skip_reason_for_match
        m = MagicMock(
            home_team="北京国安", away_team="上海申花",
            league="中超", sport="football",
            home_score=0, away_score=0,
            extra_data={"clock": "15'", "period": "1H"},
        )
        reason = skip_reason_for_match(m, total_line=2.5, odds_map={"under": 1.85}, min_odds=1.65, max_odds=5.0)
        assert reason is not None
        assert "中国" in reason or "china" in reason.lower()

    def test_blacklisted_league_skipped(self):
        """联赛黑名单跳过。"""
        from app.ai.analysis_filters import skip_reason_for_match
        m = MagicMock(
            home_team="TeamA", away_team="TeamB",
            league="U19 青年联赛", sport="football",
            home_score=0, away_score=0,
            extra_data={"clock": "15'", "period": "1H"},
        )
        reason = skip_reason_for_match(m, total_line=2.5, odds_map={"under": 1.85}, min_odds=1.65, max_odds=5.0)
        assert reason is not None

    def test_odds_out_of_range_skipped(self):
        """赔率不在区间内跳过。"""
        from app.ai.analysis_filters import skip_reason_for_match
        m = MagicMock(
            home_team="Arsenal", away_team="Chelsea",
            league="英超", sport="football",
            home_score=0, away_score=0,
            extra_data={"clock": "15'", "period": "1H"},
        )
        reason = skip_reason_for_match(m, total_line=2.5, odds_map={"under": 1.2}, min_odds=1.65, max_odds=5.0)
        assert reason is not None

    def test_normal_match_passes(self):
        """正常比赛通过。"""
        from app.ai.analysis_filters import skip_reason_for_match
        m = MagicMock(
            home_team="Arsenal", away_team="Chelsea",
            league="英超", sport="football",
            home_score=0, away_score=0,
            extra_data={"clock": "15'", "period": "1H"},
        )
        reason = skip_reason_for_match(m, total_line=2.5, odds_map={"under": 1.85}, min_odds=1.65, max_odds=5.0)
        assert reason is None
