"""测试 AI 分析引擎核心逻辑：归一化、盘口提取。"""
import pytest

from app.ai.analyzer import (
    normalize_bet_type,
    normalize_prediction,
    _infer_bet_type,
    _flatten_market_odds,
    _odds_for_pick,
    _line_for_pick,
    VALID_PREDICTIONS,
    VALID_BET_TYPES,
)


class TestNormalizeBetType:
    def test_total_aliases(self):
        assert normalize_bet_type("total") == "total"
        assert normalize_bet_type("ou") == "total"
        assert normalize_bet_type("大小") == "total"
        assert normalize_bet_type("大小球") == "total"

    def test_moneyline_removed(self):
        assert normalize_bet_type("moneyline") == ""
        assert normalize_bet_type("1x2") == ""
        assert normalize_bet_type("胜负") == ""

    def test_spread_removed(self):
        assert normalize_bet_type("spread") == ""
        assert normalize_bet_type("让球") == ""
        assert normalize_bet_type("handicap") == ""

    def test_case_insensitive(self):
        assert normalize_bet_type("TOTAL") == "total"
        assert normalize_bet_type("OU") == "total"

    def test_empty(self):
        assert normalize_bet_type("") == ""
        assert normalize_bet_type(None) == ""


class TestNormalizePrediction:
    def test_over_under(self):
        assert normalize_prediction("over") == "over"
        assert normalize_prediction("under") == "under"
        assert normalize_prediction("o") == "over"
        assert normalize_prediction("u") == "under"
        assert normalize_prediction("大") == "over"
        assert normalize_prediction("小") == "under"
        assert normalize_prediction("大球") == "over"
        assert normalize_prediction("小球") == "under"

    def test_home_away_removed(self):
        assert normalize_prediction("home") == ""
        assert normalize_prediction("away") == ""
        assert normalize_prediction("draw") == ""
        assert normalize_prediction("主") == ""
        assert normalize_prediction("客") == ""

    def test_empty(self):
        assert normalize_prediction("") == ""
        assert normalize_prediction(None) == ""


class TestInferBetType:
    def test_always_total(self):
        assert _infer_bet_type("over") == "total"
        assert _infer_bet_type("under") == "total"
        assert _infer_bet_type("home") == "total"
        assert _infer_bet_type("") == "total"


class TestValidSets:
    def test_only_total(self):
        assert VALID_BET_TYPES == {"total"}

    def test_only_over_under(self):
        assert VALID_PREDICTIONS == {"over", "under"}


class TestFlattenMarketOdds:
    def test_flat_dict(self):
        result = _flatten_market_odds({"over": 1.90, "under": 1.95})
        assert result["over"] == 1.90
        assert result["under"] == 1.95

    def test_nested_markets(self):
        odds = {"markets": {"total": {"odds": {"over": 1.85, "under": 2.05}}}}
        result = _flatten_market_odds(odds)
        assert result["over"] == 1.85

    def test_invalid_skipped(self):
        result = _flatten_market_odds({"over": 0.5, "under": 1.95})
        assert "over" not in result
        assert result["under"] == 1.95


class TestOddsForPick:
    def test_from_flat(self):
        assert _odds_for_pick({"over": 1.90}, "total", "over") == 1.90

    def test_from_nested(self):
        odds = {"markets": {"total": {"odds": {"over": 1.85}}}}
        assert _odds_for_pick(odds, "total", "over") == 1.85

    def test_missing(self):
        assert _odds_for_pick(None, "total", "over") == 0.0
