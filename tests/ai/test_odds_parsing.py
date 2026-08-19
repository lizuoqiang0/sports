"""盘口解析单元测试：全场 + 上下半场大小球 + 赔率去重。

对应 doc.md 第 9 章。
"""
import pytest
from app.services.bookmakers.plugins.ob.odds import (
    _total_from_hps,
    _half_totals_from_hps,
    parse_matches_pb,
)
from app.services.bookmakers.plugins.pinnacle.odds import (
    _half_totals_from_period,
)
from app.services.bookmakers.base import RemoteOdds
from app.models.user import BetType


class TestBetTypeEnum:
    """BetType 枚举完整性。"""

    def test_first_half_total_exists(self):
        assert hasattr(BetType, "FIRST_HALF_TOTAL")
        assert BetType.FIRST_HALF_TOTAL.value == "first_half_total"

    def test_second_half_total_exists(self):
        assert hasattr(BetType, "SECOND_HALF_TOTAL")
        assert BetType.SECOND_HALF_TOTAL.value == "second_half_total"

    def test_total_exists(self):
        assert hasattr(BetType, "TOTAL")
        assert BetType.TOTAL.value == "total"


class TestOBHalfTotalsParsing:
    """OB 插件半场大小球解析。"""

    def test_full_time_total(self, ob_hps_with_half_totals):
        """全场大小球仍正常解析。"""
        result = _total_from_hps(ob_hps_with_half_totals, mid="m1")
        assert result is not None
        assert result.bet_type == "total"
        assert result.total == 2.5
        assert result.odds_data["over"] == 1.85
        assert result.odds_data["under"] == 1.75

    def test_half_totals_count(self, ob_hps_with_half_totals):
        """上下半场各解析出 1 条。"""
        result = _half_totals_from_hps(ob_hps_with_half_totals, mid="m1")
        assert len(result) == 2

    def test_first_half_total(self, ob_hps_with_half_totals):
        """上半场大小球：bet_type + line + under + over。"""
        result = _half_totals_from_hps(ob_hps_with_half_totals, mid="m1")
        first = [r for r in result if r.bet_type == "first_half_total"]
        assert len(first) == 1
        assert first[0].total == 1.5
        assert first[0].odds_data["under"] == 1.80
        assert first[0].odds_data["over"] == 1.90
        assert first[0].odds_data["_ob"]["period"] == "上半场"

    def test_second_half_total(self, ob_hps_with_half_totals):
        """下半场大小球：bet_type + line + under + over。"""
        result = _half_totals_from_hps(ob_hps_with_half_totals, mid="m1")
        second = [r for r in result if r.bet_type == "second_half_total"]
        assert len(second) == 1
        assert second[0].total == 2.0
        assert second[0].odds_data["under"] == 1.85
        assert second[0].odds_data["over"] == 1.95
        assert second[0].odds_data["_ob"]["period"] == "下半场"

    def test_half_under_only(self, ob_hps_half_under_only):
        """半场仅有 under 时仍创建。"""
        result = _half_totals_from_hps(ob_hps_half_under_only, mid="m1")
        assert len(result) == 1
        assert result[0].bet_type == "first_half_total"
        assert result[0].odds_data["under"] == 1.80
        assert "over" not in result[0].odds_data

    def test_no_half_markets(self):
        """无半场市场时返回空列表。"""
        hps = [{
            "hpid": "2", "hpn": "全场大小",
            "hl": [{
                "hv": "2.5", "hid": "h1", "hs": "0",
                "ol": [
                    {"ot": "over", "on": "大2.5", "ov": "1.85", "oid": "o1", "os": 1},
                    {"ot": "under", "on": "小2.5", "ov": "1.75", "oid": "o2", "os": 1},
                ],
            }],
        }]
        result = _half_totals_from_hps(hps, mid="m1")
        assert len(result) == 0


class TestPinnacleHalfTotalsParsing:
    """平博插件半场大小球解析。"""

    def test_first_half_total(self, pinnacle_period1_data):
        """上半场大小球：bet_type + line + under + over。"""
        result = _half_totals_from_period(
            pinnacle_period1_data, mid="m1", sport_id=29,
            period_key="1", half_bt="first_half_total", half_label="上半场",
        )
        assert len(result) == 1
        assert result[0].bet_type == "first_half_total"
        assert result[0].total == 1.5
        assert result[0].odds_data["under"] == 1.80
        assert result[0].odds_data["over"] == 1.90
        assert result[0].odds_data["_site"]["period"] == "上半场"

    def test_second_half_under_only(self, pinnacle_period2_under_only):
        """下半场仅有 under 时仍创建。"""
        result = _half_totals_from_period(
            pinnacle_period2_under_only, mid="m2", sport_id=29,
            period_key="2", half_bt="second_half_total", half_label="下半场",
        )
        assert len(result) == 1
        assert result[0].bet_type == "second_half_total"
        assert result[0].total == 2.0
        assert result[0].odds_data["under"] == 1.85
        assert "over" not in result[0].odds_data

    def test_empty_period(self):
        """空 period 返回空列表。"""
        result = _half_totals_from_period(
            [], mid="m1", sport_id=29,
            period_key="1", half_bt="first_half_total", half_label="上半场",
        )
        assert len(result) == 0

    def test_no_total_block(self):
        """period 无 total block 时返回空。"""
        pdata = [[], [], []]  # 三个空 block
        result = _half_totals_from_period(
            pdata, mid="m1", sport_id=29,
            period_key="1", half_bt="first_half_total", half_label="上半场",
        )
        assert len(result) == 0


class TestVenueLiveTotalParsing:
    """venue_live.py _parse_total_from_raw 测试。"""

    def test_parse_full_over_under(self):
        from app.services.bookmakers.venue_live import _parse_total_from_raw
        raw = "大 2.5 1.85 小 2.5 1.75"
        result = _parse_total_from_raw(raw, sport="football")
        assert result is not None
        assert result["line"] == 2.5
        assert result["over"] == 1.85
        assert result["under"] == 1.75

    def test_parse_under_only(self):
        from app.services.bookmakers.venue_live import _parse_total_from_raw
        raw = "小 2.5 1.75"
        result = _parse_total_from_raw(raw, sport="football")
        assert result is not None
        assert result["line"] == 2.5
        assert result["under"] == 1.75

    def test_parse_no_total_keywords(self):
        from app.services.bookmakers.venue_live import _parse_total_from_raw
        raw = "主胜 1.85 客胜 2.10"
        result = _parse_total_from_raw(raw, sport="football")
        assert result is None

    def test_football_line_range(self):
        """足球盘口线范围 0.5-12。"""
        from app.services.bookmakers.venue_live import _parse_total_from_raw
        # line=0.3 (< 0.5) -> None
        assert _parse_total_from_raw("大 0.3 1.85 小 0.3 1.75", sport="football") is None
        # line=15 (> 12) -> None
        assert _parse_total_from_raw("大 15 1.85 小 15 1.75", sport="football") is None

    def test_basketball_line_range(self):
        """篮球盘口线范围：宽松兜底 0.5-280。"""
        from app.services.bookmakers.venue_live import _parse_total_from_raw
        # line=0.3 (< 0.5, 连宽松兜底都拒绝) -> None
        assert _parse_total_from_raw("大 0.3 1.85 小 0.3 1.75", sport="basketball") is None
        # line=300 (> 280, 宽松兜底也拒绝) -> None
        assert _parse_total_from_raw("大 300 1.85 小 300 1.75", sport="basketball") is None
        # line=50 (在宽松兜底 0.5-280 内) -> 通过
        result = _parse_total_from_raw("大 50 1.85 小 50 1.75", sport="basketball")
        assert result is not None
        assert result["line"] == 50.0

    def test_under_range_check(self):
        """under 赔率 <= 1 时返回 None。"""
        from app.services.bookmakers.venue_live import _parse_total_from_raw
        # under=0.9 (<= 1) -> None
        result = _parse_total_from_raw("大 2.5 1.85 小 2.5 0.90", sport="football")
        assert result is None


class TestOddsDedup:
    """赔率去重逻辑。"""

    def test_dedup_same_bet_type_and_line(self):
        """同 bet_type + 同盘口线只保留最新一条。"""
        odds_list = [
            RemoteOdds(bet_type="total", odds_data={"under": 1.70}, total=2.5),
            RemoteOdds(bet_type="total", odds_data={"under": 1.75}, total=2.5),  # 更新
            RemoteOdds(bet_type="total", odds_data={"under": 1.80}, total=3.0),  # 不同线
            RemoteOdds(bet_type="first_half_total", odds_data={"under": 1.80}, total=1.5),
        ]
        # 模拟去重逻辑
        _dedup: dict[str, RemoteOdds] = {}
        for o in odds_list:
            key = f"{o.bet_type}|{o.total or 0}|{o.spread or 0}"
            _dedup[key] = o
        result = list(_dedup.values())
        # 3 条：total|2.5, total|3.0, first_half_total|1.5
        assert len(result) == 3
        # total|2.5 的 under 应为最新的 1.75
        t25 = [o for o in result if o.bet_type == "total" and o.total == 2.5][0]
        assert t25.odds_data["under"] == 1.75

    def test_dedup_different_bet_types(self):
        """不同 bet_type 不去重。"""
        odds_list = [
            RemoteOdds(bet_type="total", odds_data={"under": 1.75}, total=2.5),
            RemoteOdds(bet_type="first_half_total", odds_data={"under": 1.80}, total=2.5),
            RemoteOdds(bet_type="second_half_total", odds_data={"under": 1.85}, total=2.5),
        ]
        _dedup: dict[str, RemoteOdds] = {}
        for o in odds_list:
            key = f"{o.bet_type}|{o.total or 0}|{o.spread or 0}"
            _dedup[key] = o
        result = list(_dedup.values())
        assert len(result) == 3
