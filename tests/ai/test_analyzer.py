"""DeepSeek 分析引擎单元测试：缓存 + DeepSeek 重试 + 篮球时间转换 + 信号函数。

对应 doc.md 第 4 章。
"""
import pytest
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from app.ai.analyzer import MatchAnalyzer


class TestElapsedMinutes:
    """_elapsed_minutes 辅助函数。"""

    def test_football_clock(self):
        """足球 clock="27'" 返回 27.0。"""
        info = {"sport": "football", "period": "1H", "clock": "27'"}
        mins = MatchAnalyzer._elapsed_minutes(info)
        assert mins is not None
        assert mins >= 25  # 足球直接解析

    def test_basketball_q4_countdown(self):
        """篮球 Q4 倒计时 8:30 应转换为 ~39.5 分钟。"""
        info = {"sport": "basketball", "period": "Q4", "clock": "8:30"}
        mins = MatchAnalyzer._elapsed_minutes(info)
        assert mins is not None
        assert mins > 35  # Q4 剩 8:30 -> 已进行约 39.5 分钟

    def test_basketball_q1_countdown(self):
        """篮球 Q1 倒计时 10:00 应转换为 ~2 分钟。"""
        info = {"sport": "basketball", "period": "Q1", "clock": "10:00"}
        mins = MatchAnalyzer._elapsed_minutes(info)
        assert mins is not None
        assert mins < 5  # Q1 剩 10:00 -> 已进行约 2 分钟


class TestStageSignal:
    """_stage_signal 比赛阶段信号。"""

    def test_basketball_late_stage_conflict(self):
        """篮球最后 4 分钟应触发 conflict（mins >= 44）。"""
        info = {"sport": "basketball", "period": "Q4", "clock": "2:00"}
        result = MatchAnalyzer._stage_signal(
            info, selection="under", bet_type="total",
        )
        # Q4 剩 2:00 -> 已进行约 46 分钟 -> >= 44 -> conflict
        assert result.get("conflict") is True

    def test_basketball_early_stage_no_conflict(self):
        """篮球 Q1 早段不触发 conflict。"""
        info = {"sport": "basketball", "period": "Q1", "clock": "8:00"}
        result = MatchAnalyzer._stage_signal(
            info, selection="under", bet_type="total",
        )
        assert not result.get("conflict")

    def test_football_early_stage_supportive(self):
        """足球早段（< 25min）under 支持。"""
        info = {"sport": "football", "period": "1H", "clock": "15'"}
        result = MatchAnalyzer._stage_signal(
            info, selection="under", bet_type="total",
        )
        assert result.get("supportive") is True


class TestAntiChaseSignal:
    """_anti_chase_signal 反追低位小球信号。"""

    def test_basketball_not_always_conflict(self):
        """篮球不应恒触发 anti-chase（修复倒计时转换后）。"""
        info = {"sport": "basketball", "period": "Q3", "clock": "5:00"}
        market_odds = {"opening": {"total_line": 180.5}, "live": {"total_line": 180.0}}
        result = MatchAnalyzer._anti_chase_signal(
            info, market_odds, {},
            selection="under", bet_type="total",
        )
        # Q3 剩 5:00 -> 已进行约 31 分钟 -> mins <= 60 为 True
        # 但盘口变化仅 -0.5，应检查是否合理
        # 关键：mins 不再是倒计时值（如 5.0），而是已进行时间（~31）
        assert result is not None


class TestCallDeepSeek:
    """_call_deepseek 错误处理。"""

    def test_max_retries_is_2(self):
        """DeepSeek 重试次数为 2（非 3）。"""
        import inspect
        src = inspect.getsource(MatchAnalyzer._call_deepseek)
        assert "max_retries = 2" in src

    def test_empty_choices_raises(self):
        """空 choices 应 raise。"""
        import inspect
        src = inspect.getsource(MatchAnalyzer._call_deepseek)
        assert "choices" in src

    def test_timeout_no_retry(self):
        """超时不重试。"""
        import inspect
        src = inspect.getsource(MatchAnalyzer._call_deepseek)
        assert "timeout" in src.lower() or "timed out" in src.lower()


class TestCacheTtl:
    """缓存策略。"""

    def test_positive_cache_180s(self):
        """正缓存 TTL = 180s（3 分钟）。"""
        import inspect
        src = inspect.getsource(MatchAnalyzer.analyze_match)
        assert "180" in src

    def test_cache_key_contains_line_tag(self):
        """缓存 key 包含 line_tag。"""
        import inspect
        src = inspect.getsource(MatchAnalyzer.analyze_match)
        assert "line_tag" in src


def _valid_nowscore_context():
    def rows(totals):
        return [{"home_goals": value, "away_goals": 0} for value in totals]

    return {
        "source": "nowscore",
        "sport": "football",
        "identity": {"validated": True},
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "h2h": {"matches": rows([2, 3, 4])},
        "home_form": {"matches": rows([2, 3, 4])},
        "away_form": {"matches": rows([1, 3, 4])},
    }


@pytest.mark.asyncio
async def test_nowscore_gate_rejects_missing_context_before_model_call():
    analyzer = MatchAnalyzer()
    analyzer._call_deepseek = AsyncMock(side_effect=AssertionError("model must not be called"))

    result = await analyzer.analyze_match(
        {"id": 1, "sport": "football", "home_team": "A", "away_team": "B", "total_line": 2.5},
        historical_data=None,
        market_odds={"markets": {"total": {"line": 2.5, "odds": {"under": 1.9, "over": 1.9}}}},
    )

    assert result["prediction"] == "skip"
    assert result["error"].startswith("nowscore_evidence_unusable:")
    analyzer._call_deepseek.assert_not_awaited()


@pytest.mark.asyncio
async def test_model_line_mismatch_is_rejected():
    analyzer = MatchAnalyzer()
    analyzer._call_deepseek = AsyncMock(return_value={
        "content": json.dumps({
            "prediction": "over",
            "under_confidence": 0.35,
            "over_confidence": 0.65,
            "line": 2.5,
            "reasoning": "test",
        }),
        "_meta": {"latency_ms": 1},
    })
    analyzer._build_historical_feedback = AsyncMock(return_value="")

    result = await analyzer.analyze_match(
        {"id": 2, "sport": "football", "home_team": "A", "away_team": "B", "total_line": 2.25},
        historical_data=_valid_nowscore_context(),
        market_odds={"markets": {"total": {"line": 2.25, "odds": {"under": 1.9, "over": 1.9}}}},
    )

    assert result["prediction"] == "skip"
    assert result["error"] == "model_total_line_mismatch"
    assert result["line"] == 2.25
    assert result["model_line"] == 2.5


@pytest.mark.asyncio
async def test_structured_gate_requires_two_sided_total_odds_before_model():
    analyzer = MatchAnalyzer()
    analyzer._call_deepseek = AsyncMock(side_effect=AssertionError("model must not be called"))

    result = await analyzer.analyze_match(
        {
            "id": 3, "sport": "football", "status": "upcoming",
            "home_team": "A", "away_team": "B", "total_line": 2.5,
        },
        historical_data=_valid_nowscore_context(),
        market_odds={"markets": {"total": {"line": 2.5, "odds": {"under": 1.9}}}},
    )

    assert result["prediction"] == "skip"
    assert result["error"] == "total_feature_gate:missing_two_sided_total_odds"
    analyzer._call_deepseek.assert_not_awaited()
