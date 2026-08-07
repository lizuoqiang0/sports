"""测试 AI 分析引擎核心逻辑：去 vig、EV 计算、归一化、共识聚合。"""
import pytest

from app.ai.analyzer import (
    normalize_bet_type,
    normalize_prediction,
    _infer_bet_type,
    _devig_odds,
    _devig_ev,
    _flatten_market_odds,
    _odds_for_pick,
    _line_for_pick,
    VALID_PREDICTIONS,
    VALID_BET_TYPES,
)


class TestNormalizeBetType:
    def test_english_aliases(self):
        assert normalize_bet_type("total") == "total"
        assert normalize_bet_type("ou") == "total"
        assert normalize_bet_type("moneyline") == "moneyline"
        assert normalize_bet_type("1x2") == "moneyline"
        assert normalize_bet_type("ml") == "moneyline"
        assert normalize_bet_type("spread") == "spread"
        assert normalize_bet_type("ah") == "spread"
        assert normalize_bet_type("handicap") == "spread"

    def test_chinese_aliases(self):
        assert normalize_bet_type("大小") == "total"
        assert normalize_bet_type("大小球") == "total"
        assert normalize_bet_type("胜负") == "moneyline"
        assert normalize_bet_type("独赢") == "moneyline"
        assert normalize_bet_type("让球") == "spread"
        assert normalize_bet_type("让分") == "spread"

    def test_case_insensitive(self):
        assert normalize_bet_type("TOTAL") == "total"
        assert normalize_bet_type("MoneyLine") == "moneyline"
        assert normalize_bet_type("SPREAD") == "spread"

    def test_empty_and_invalid(self):
        assert normalize_bet_type("") == ""
        assert normalize_bet_type(None) == ""
        assert normalize_bet_type("unknown") == ""


class TestNormalizePrediction:
    def test_english(self):
        assert normalize_prediction("over") == "over"
        assert normalize_prediction("under") == "under"
        assert normalize_prediction("home") == "home"
        assert normalize_prediction("away") == "away"
        assert normalize_prediction("draw") == "draw"

    def test_chinese(self):
        assert normalize_prediction("大") == "over"
        assert normalize_prediction("小") == "under"
        assert normalize_prediction("大球") == "over"
        assert normalize_prediction("小球") == "under"
        assert normalize_prediction("主") == "home"
        assert normalize_prediction("客") == "away"
        assert normalize_prediction("平") == "draw"
        assert normalize_prediction("主胜") == "home"
        assert normalize_prediction("客胜") == "away"
        assert normalize_prediction("平局") == "draw"

    def test_short_codes(self):
        assert normalize_prediction("o") == "over"
        assert normalize_prediction("u") == "under"
        assert normalize_prediction("h") == "home"
        assert normalize_prediction("a") == "away"
        assert normalize_prediction("d") == "draw"
        assert normalize_prediction("x") == "draw"
        assert normalize_prediction("1") == "home"
        assert normalize_prediction("2") == "away"

    def test_bet_type_constraint(self):
        # total 只允许 over/under
        assert normalize_prediction("over", bet_type="total") == "over"
        assert normalize_prediction("home", bet_type="total") == ""
        # moneyline 只允许 home/away/draw
        assert normalize_prediction("home", bet_type="moneyline") == "home"
        assert normalize_prediction("over", bet_type="moneyline") == ""
        # spread 只允许 home/away
        assert normalize_prediction("home", bet_type="spread") == "home"
        assert normalize_prediction("draw", bet_type="spread") == ""

    def test_empty(self):
        assert normalize_prediction("") == ""
        assert normalize_prediction(None) == ""


class TestInferBetType:
    def test_over_under_infer_total(self):
        assert _infer_bet_type("over") == "total"
        assert _infer_bet_type("under") == "total"

    def test_draw_infer_moneyline(self):
        assert _infer_bet_type("draw") == "moneyline"

    def test_home_away_ambiguous(self):
        assert _infer_bet_type("home") == ""
        assert _infer_bet_type("away") == ""

    def test_declared_takes_priority(self):
        assert _infer_bet_type("over", declared="spread") == "spread"
        assert _infer_bet_type("home", declared="moneyline") == "moneyline"


class TestDevigOdds:
    def test_two_way_market(self):
        """1/1.90 + 1/1.95 = 1.0263 -> vig ~2.6%"""
        result = _devig_odds({"over": 1.90, "under": 1.95})
        inv_sum = 1 / 1.90 + 1 / 1.95  # ~1.0263
        # 公平赔率应略高于原始赔率（去除了 margin）
        assert result["over"] > 1.90
        assert result["under"] > 1.95
        # 去 vig 后 1/odds_sum 应等于 1.0（4 位小数舍入有微小误差）
        total_inv = 1 / result["over"] + 1 / result["under"]
        assert abs(total_inv - 1.0) < 1e-4

    def test_three_way_market(self):
        result = _devig_odds({"home": 2.50, "draw": 3.30, "away": 2.80})
        total_inv = 1 / result["home"] + 1 / result["draw"] + 1 / result["away"]
        assert abs(total_inv - 1.0) < 1e-4

    def test_empty_or_invalid(self):
        assert _devig_odds({}) == {}
        assert _devig_odds({"a": 0.5}) == {"a": 0.5}  # <= 1.0 被跳过


class TestDevigEv:
    def test_positive_ev(self):
        """conf=0.60, odds=1.90 -> raw EV = 0.60*1.90-1 = 0.14"""
        ev = _devig_ev(0.60, 1.90, {"over": 1.90, "under": 1.95}, selection="over")
        # 去 vig 后公平赔率 > 1.90, EV 应 > raw EV
        assert ev > 0.14 - 0.01  # 允许小误差

    def test_selection_direct_lookup(self):
        """修复后：直接按 selection 查找公平赔率"""
        ev = _devig_ev(0.70, 1.80, {"over": 1.80, "under": 2.00}, selection="over")
        fair = _devig_odds({"over": 1.80, "under": 2.00})
        expected = round(0.70 * fair["over"] - 1, 4)
        assert ev == expected

    def test_no_selection_fallback(self):
        """无 selection 时回退到原始赔率"""
        ev = _devig_ev(0.60, 1.90, {"over": 1.90, "under": 1.95})
        assert ev == round(0.60 * 1.90 - 1, 4)

    def test_empty_odds(self):
        assert _devig_ev(0.60, 1.90, {}) == round(0.60 * 1.90 - 1, 4)

    def test_invalid_odds(self):
        assert _devig_ev(0.60, 0, {"over": 1.90}) == 0.0


class TestFlattenMarketOdds:
    def test_flat_dict(self):
        odds = {"over": 1.90, "under": 1.95, "home": 2.00}
        result = _flatten_market_odds(odds)
        assert result["over"] == 1.90
        assert result["under"] == 1.95
        assert result["home"] == 2.00

    def test_nested_markets(self):
        odds = {
            "markets": {
                "total": {"odds": {"over": 1.85, "under": 2.05}},
                "moneyline": {"odds": {"home": 1.50, "draw": 4.00, "away": 6.00}},
            }
        }
        result = _flatten_market_odds(odds)
        assert result["over"] == 1.85
        assert result["under"] == 2.05
        assert result["home"] == 1.50

    def test_invalid_odds_skipped(self):
        odds = {"over": 0.5, "under": 1.95}
        result = _flatten_market_odds(odds)
        assert "over" not in result
        assert result["under"] == 1.95


class TestOddsForPick:
    def test_from_flat(self):
        market_odds = {"over": 1.90, "under": 1.95}
        assert _odds_for_pick(market_odds, "total", "over") == 1.90

    def test_from_nested(self):
        market_odds = {
            "markets": {
                "total": {"odds": {"over": 1.85, "under": 2.05}},
            }
        }
        assert _odds_for_pick(market_odds, "total", "over") == 1.85

    def test_missing(self):
        assert _odds_for_pick(None, "total", "over") == 0.0
        assert _odds_for_pick({}, "total", "over") == 0.0


class TestLineForPick:
    def test_from_nested(self):
        market_odds = {"markets": {"total": {"line": 2.5, "over": 1.85, "under": 2.05}}}
        assert _line_for_pick(market_odds, None, "total") == 2.5

    def test_from_match_info(self):
        match_info = {"total_line": 3.0}
        assert _line_for_pick(None, match_info, "total") == 3.0
        assert _line_for_pick(None, match_info, "spread") is None

    def test_missing(self):
        assert _line_for_pick(None, None, "total") is None
