"""手动投注（one_click_bet）单元测试。

验证手动投注与自动投注共用 execute_bet 的统一链路：
  - 推荐缓存命中路径
  - 无缓存时 fallback 到 analyze_and_recommend
  - 策略闸门校验（should_bet / 球队排除 / 仓位上下限）
  - BetDecision 构造正确性
  - execute_bet 调用 + 结果返回
  - execute_bet 失败时 HTTPException
  - 赛事不存在 / 推荐无方向 等异常路径
"""
import sys
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from decimal import Decimal
from fastapi import HTTPException

# 预注入 mock 模块，避免 redis / websockets 等未安装时导入失败
for _mod in ("redis", "redis.asyncio", "websockets", "websockets.legacy", "socksio"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

# Mock AsyncOpenAI 构造函数，避免初始化真实 HTTP 客户端（SOCKS 代理等环境问题）
import openai  # noqa: E402
openai.AsyncOpenAI = MagicMock()

import app.core.cache  # noqa: E402

from app.ai.strategy import StrategyConfig, BetDecision
from app.ai.bet_executor import BetExecResult
from app.api.ai_bets import OneClickBetRequest, one_click_bet


# ── fixtures ──


@pytest.fixture
def strat_cfg():
    return StrategyConfig(
        name="simple",
        max_bet_amount=100.0,
        max_daily_bets=10,
        stop_loss=500.0,
        take_profit=1000.0,
        use_llm_analysis=True,
        min_confidence=0.47,
        min_odds=1.65,
        max_odds=5.0,
    )


@pytest.fixture
def mock_user():
    u = MagicMock()
    u.id = 1
    u.balance = Decimal("5000")
    u.ai_enabled = True
    return u


@pytest.fixture
def mock_match():
    m = MagicMock()
    m.id = 1001
    m.sport = "football"
    m.league = "英超"
    m.home_team = "Arsenal"
    m.away_team = "Chelsea"
    m.external_id = "pinnacle:1634071712"
    m.extra_data = {"ids": {"pinnacle": "pinnacle:1634071712"}}
    return m


@pytest.fixture
def mock_rec():
    """可投推荐（should_bet=True）。"""
    return {
        "match_id": 1001,
        "home_team": "Arsenal",
        "away_team": "Chelsea",
        "sport": "football",
        "recommendation": {
            "should_bet": True,
            "selection": "under",
            "confidence": 0.65,
            "odds": 1.85,
            "line": 2.5,
            "bet_type": "total",
            "provider_code": "pinnacle",
            "reasoning": "防守稳固",
            "suggested_stake": 50,
        },
        "markets": [],
    }


def _mk_db(match=None):
    """构造 mock db，execute 返回含 match 的结果。"""
    db = MagicMock()
    db.in_transaction = MagicMock(return_value=False)
    db.add = MagicMock()
    db.flush = AsyncMock()
    match_result = MagicMock()
    match_result.scalar_one_or_none.return_value = match
    db.execute = AsyncMock(return_value=match_result)
    return db


def _mk_bet_result(ok=True, **kwargs):
    """构造 BetExecResult。"""
    defaults = {
        "bet_id": 999,
        "provider_code": "pinnacle",
        "provider_label": "平博",
        "stake": Decimal("50"),
        "odds": 1.85,
        "line": 2.5,
        "external_bet_id": "p123",
        "potential_payout": Decimal("92.50"),
        "site_balance": 950.0,
        "message": "",
    }
    defaults.update(kwargs)
    return BetExecResult(ok=ok, **defaults)


# ── 赛事不存在 ──


class TestMatchNotFound:
    """赛事不存在时返回 404。"""

    @pytest.mark.asyncio
    async def test_404_when_match_missing(self, mock_user, strat_cfg):
        db = _mk_db(match=None)
        req = OneClickBetRequest(stake=50)

        with patch("app.ai.strategy.load_fresh_strategy", new_callable=AsyncMock) as gls:
            gls.return_value = (MagicMock(), strat_cfg)
            with pytest.raises(HTTPException) as exc:
                await one_click_bet(1001, req, mock_user, db)

        assert exc.value.status_code == 404
        assert "赛事不存在" in exc.value.detail


# ── 推荐缓存命中 ──


class TestCachedRecommendation:
    """缓存命中推荐，不重新分析。"""

    @pytest.mark.asyncio
    async def test_uses_cached_rec(
        self, mock_user, mock_match, mock_rec, strat_cfg
    ):
        db = _mk_db(match=mock_match)
        req = OneClickBetRequest(stake=50)

        with patch("app.ai.strategy.load_fresh_strategy", new_callable=AsyncMock) as gls, \
             patch("app.ai.strategy_gates.gate_recommendation_for_place", new_callable=AsyncMock) as gg, \
             patch("app.ai.bet_executor.execute_bet", new_callable=AsyncMock) as ge, \
             patch("app.api.ai_bets.analyze_and_recommend", new_callable=AsyncMock) as ga, \
             patch("app.core.cache.cache") as mock_cache:

            gls.return_value = (MagicMock(), strat_cfg)
            gg.return_value = (True, "", Decimal("50"), strat_cfg)
            ge.return_value = _mk_bet_result()
            ga.return_value = mock_rec

            # cache 命中
            mock_cache.get_json = AsyncMock(return_value=mock_rec)

            resp = await one_click_bet(1001, req, mock_user, db)

        data = resp.data
        assert data["success_count"] == 1
        assert data["bets"][0]["provider"] == "平博"
        assert data["bets"][0]["selection"] == "under"
        assert data["bets"][0]["odds"] == 1.85


# ── 无缓存 fallback 到 analyze_and_recommend ──


class TestFallbackAnalysis:
    """无缓存推荐时 fallback 到 analyze_and_recommend。"""

    @pytest.mark.asyncio
    async def test_fallback_to_analyze(
        self, mock_user, mock_match, mock_rec, strat_cfg
    ):
        db = _mk_db(match=mock_match)
        req = OneClickBetRequest(stake=50)

        with patch("app.ai.strategy.load_fresh_strategy", new_callable=AsyncMock) as gls, \
             patch("app.ai.strategy_gates.gate_recommendation_for_place", new_callable=AsyncMock) as gg, \
             patch("app.ai.bet_executor.execute_bet", new_callable=AsyncMock) as ge, \
             patch("app.api.ai_bets.analyze_and_recommend", new_callable=AsyncMock) as ga, \
             patch("app.core.cache.cache") as mock_cache:

            gls.return_value = (MagicMock(), strat_cfg)
            gg.return_value = (True, "", Decimal("50"), strat_cfg)
            ge.return_value = _mk_bet_result()
            ga.return_value = mock_rec

            # cache 未命中
            mock_cache.get_json = AsyncMock(return_value=None)

            resp = await one_click_bet(1001, req, mock_user, db)

        # analyze 被调用
        ga.assert_called_once_with(1001, mock_user.id)
        assert resp.data["success_count"] == 1

    @pytest.mark.asyncio
    async def test_analyze_failure_raises_400(
        self, mock_user, mock_match, strat_cfg
    ):
        db = _mk_db(match=mock_match)
        req = OneClickBetRequest(stake=50)

        with patch("app.ai.strategy.load_fresh_strategy", new_callable=AsyncMock) as gls, \
             patch("app.api.ai_bets.analyze_and_recommend", new_callable=AsyncMock) as ga, \
             patch("app.core.cache.cache") as mock_cache:

            gls.return_value = (MagicMock(), strat_cfg)
            ga.side_effect = RuntimeError("LLM 超时")
            mock_cache.get_json = AsyncMock(return_value=None)

            with pytest.raises(HTTPException) as exc:
                await one_click_bet(1001, req, mock_user, db)

        assert exc.value.status_code == 400
        assert "AI分析失败" in exc.value.detail


# ── 策略闸门校验 ──


class TestGateValidation:
    """gate_recommendation_for_place 校验。"""

    @pytest.mark.asyncio
    async def test_gate_rejected(
        self, mock_user, mock_match, mock_rec, strat_cfg
    ):
        """策略闸门未通过 -> 400。"""
        db = _mk_db(match=mock_match)
        req = OneClickBetRequest(stake=50)

        with patch("app.ai.strategy.load_fresh_strategy", new_callable=AsyncMock) as gls, \
             patch("app.ai.strategy_gates.gate_recommendation_for_place", new_callable=AsyncMock) as gg, \
             patch("app.api.ai_bets.analyze_and_recommend", new_callable=AsyncMock) as ga, \
             patch("app.core.cache.cache") as mock_cache:

            gls.return_value = (MagicMock(), strat_cfg)
            gg.return_value = (False, "球队在排除名单中", Decimal("0"), strat_cfg)
            ga.return_value = mock_rec
            mock_cache.get_json = AsyncMock(return_value=mock_rec)

            with pytest.raises(HTTPException) as exc:
                await one_click_bet(1001, req, mock_user, db)

        assert exc.value.status_code == 400
        assert "排除" in exc.value.detail

    @pytest.mark.asyncio
    async def test_stake_below_minimum(
        self, mock_user, mock_match, mock_rec, strat_cfg
    ):
        """仓位低于下限 -> 400。"""
        db = _mk_db(match=mock_match)
        req = OneClickBetRequest(stake=0.01)

        with patch("app.ai.strategy.load_fresh_strategy", new_callable=AsyncMock) as gls, \
             patch("app.api.ai_bets.analyze_and_recommend", new_callable=AsyncMock) as ga, \
             patch("app.core.cache.cache") as mock_cache:

            gls.return_value = (MagicMock(), strat_cfg)
            ga.return_value = mock_rec
            mock_cache.get_json = AsyncMock(return_value=mock_rec)

            with pytest.raises(HTTPException) as exc:
                await one_click_bet(1001, req, mock_user, db)

        assert exc.value.status_code == 400
        assert "金额" in exc.value.detail

    @pytest.mark.asyncio
    async def test_stake_above_maximum(
        self, mock_user, mock_match, mock_rec, strat_cfg
    ):
        """仓位超过上限 -> 400。"""
        db = _mk_db(match=mock_match)
        req = OneClickBetRequest(stake=999)

        with patch("app.ai.strategy.load_fresh_strategy", new_callable=AsyncMock) as gls, \
             patch("app.api.ai_bets.analyze_and_recommend", new_callable=AsyncMock) as ga, \
             patch("app.core.cache.cache") as mock_cache:

            gls.return_value = (MagicMock(), strat_cfg)
            ga.return_value = mock_rec
            mock_cache.get_json = AsyncMock(return_value=mock_rec)

            with pytest.raises(HTTPException) as exc:
                await one_click_bet(1001, req, mock_user, db)

        assert exc.value.status_code == 400
        assert "上限" in exc.value.detail


# ── should_bet=False 拦截 ──


class TestShouldBetFalse:
    """推荐 should_bet=False -> 400。"""

    @pytest.mark.asyncio
    async def test_rejects_when_should_bet_false(
        self, mock_user, mock_match, mock_rec, strat_cfg
    ):
        mock_rec["recommendation"]["should_bet"] = False
        db = _mk_db(match=mock_match)
        req = OneClickBetRequest(stake=50)

        with patch("app.ai.strategy.load_fresh_strategy", new_callable=AsyncMock) as gls, \
             patch("app.ai.strategy_gates.gate_recommendation_for_place", new_callable=AsyncMock) as gg, \
             patch("app.api.ai_bets.analyze_and_recommend", new_callable=AsyncMock) as ga, \
             patch("app.core.cache.cache") as mock_cache:

            gls.return_value = (MagicMock(), strat_cfg)
            # gate 不会通过 should_bet=False，返回 False
            gg.return_value = (False, "策略未通过", Decimal("0"), strat_cfg)
            ga.return_value = mock_rec
            mock_cache.get_json = AsyncMock(return_value=mock_rec)

            with pytest.raises(HTTPException) as exc:
                await one_click_bet(1001, req, mock_user, db)

        assert exc.value.status_code == 400


# ── execute_bet 调用 + 结果返回 ──


class TestExecuteBetCall:
    """验证 one_click_bet 正确调用 execute_bet 并返回结果。"""

    @pytest.mark.asyncio
    async def test_calls_execute_bet_with_decision(
        self, mock_user, mock_match, mock_rec, strat_cfg
    ):
        """验证传入 execute_bet 的 BetDecision 字段正确。"""
        db = _mk_db(match=mock_match)
        req = OneClickBetRequest(stake=50)

        captured_decision = None

        async def _capture(db, user, match, decision, strat, *, is_auto):
            nonlocal captured_decision
            captured_decision = decision
            return _mk_bet_result()

        with patch("app.ai.strategy.load_fresh_strategy", new_callable=AsyncMock) as gls, \
             patch("app.ai.strategy_gates.gate_recommendation_for_place", new_callable=AsyncMock) as gg, \
             patch("app.ai.bet_executor.execute_bet", new=_capture), \
             patch("app.api.ai_bets.analyze_and_recommend", new_callable=AsyncMock) as ga, \
             patch("app.core.cache.cache") as mock_cache:

            gls.return_value = (MagicMock(), strat_cfg)
            gg.return_value = (True, "", Decimal("50"), strat_cfg)
            ga.return_value = mock_rec
            mock_cache.get_json = AsyncMock(return_value=mock_rec)

            resp = await one_click_bet(1001, req, mock_user, db)

        # 验证 BetDecision 构造正确
        assert captured_decision is not None
        assert captured_decision.match_id == 1001
        assert captured_decision.selection == "under"
        assert captured_decision.confidence == 0.65
        assert captured_decision.suggested_stake == Decimal("50")
        assert captured_decision.should_bet is True
        assert captured_decision.bet_type == "total"
        assert captured_decision.provider_code == "pinnacle"
        assert captured_decision.odds == 1.85
        assert captured_decision.line == 2.5

        # 验证返回结构
        data = resp.data
        assert data["bets"][0]["market"] == "total"
        assert data["bets"][0]["external_bet_id"] == "p123"
        assert data["bets"][0]["bet_id"] == 999
        assert data["total_stake"] == 50.0
        assert data["provider"] == "平博"

    @pytest.mark.asyncio
    async def test_execute_bet_failure_raises_400(
        self, mock_user, mock_match, mock_rec, strat_cfg
    ):
        """execute_bet 返回 ok=False -> HTTPException 400。"""
        db = _mk_db(match=mock_match)
        req = OneClickBetRequest(stake=50)

        with patch("app.ai.strategy.load_fresh_strategy", new_callable=AsyncMock) as gls, \
             patch("app.ai.strategy_gates.gate_recommendation_for_place", new_callable=AsyncMock) as gg, \
             patch("app.ai.bet_executor.execute_bet", new_callable=AsyncMock) as ge, \
             patch("app.api.ai_bets.analyze_and_recommend", new_callable=AsyncMock) as ga, \
             patch("app.core.cache.cache") as mock_cache:

            gls.return_value = (MagicMock(), strat_cfg)
            gg.return_value = (True, "", Decimal("50"), strat_cfg)
            ge.return_value = _mk_bet_result(ok=False, message="站点未连接")
            ga.return_value = mock_rec
            mock_cache.get_json = AsyncMock(return_value=mock_rec)

            with pytest.raises(HTTPException) as exc:
                await one_click_bet(1001, req, mock_user, db)

        assert exc.value.status_code == 400
        assert "站点未连接" in exc.value.detail


# ── is_auto=False 验证 ──


class TestIsAutoFlag:
    """手动投注必须以 is_auto=False 调用 execute_bet。"""

    @pytest.mark.asyncio
    async def test_is_auto_false(
        self, mock_user, mock_match, mock_rec, strat_cfg
    ):
        db = _mk_db(match=mock_match)
        req = OneClickBetRequest(stake=50)

        captured_is_auto = None

        async def _capture(db, user, match, decision, strat, *, is_auto):
            nonlocal captured_is_auto
            captured_is_auto = is_auto
            return _mk_bet_result()

        with patch("app.ai.strategy.load_fresh_strategy", new_callable=AsyncMock) as gls, \
             patch("app.ai.strategy_gates.gate_recommendation_for_place", new_callable=AsyncMock) as gg, \
             patch("app.ai.bet_executor.execute_bet", new=_capture), \
             patch("app.api.ai_bets.analyze_and_recommend", new_callable=AsyncMock) as ga, \
             patch("app.core.cache.cache") as mock_cache:

            gls.return_value = (MagicMock(), strat_cfg)
            gg.return_value = (True, "", Decimal("50"), strat_cfg)
            ga.return_value = mock_rec
            mock_cache.get_json = AsyncMock(return_value=mock_rec)

            await one_click_bet(1001, req, mock_user, db)

        assert captured_is_auto is False
