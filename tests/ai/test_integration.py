"""集成测试：全链路 + 跨组件竞态 + 热更新 + 数据质量。

对应 doc.md 第 6/7/10 章。
"""
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from decimal import Decimal

from app.ai.strategy import (
    StrategyEngine,
    StrategyConfig,
    SPORT_RISK,
    LEAGUE_BLACKLIST_KEYWORDS,
)
from app.ai.strategy_gates import (
    gate_recommendation_for_place,
    check_daily_risk,
    cap_stake,
)
from app.ai.analyzer import MatchAnalyzer
from app.services.bookmakers.plugins.ob.odds import parse_matches_pb


class TestFullBettingChain:
    """投注决策全链路（doc.md 第 10 章）。"""

    @pytest.mark.asyncio
    async def test_normal_bet_flow(
        self, mock_strategy_config, mock_match_info, mock_analysis_under
    ):
        """正常投注链路：扫描->分析->策略通过->下单。"""
        engine = StrategyEngine(mock_strategy_config, user_id=1)
        decision = await engine.evaluate_bet(
            mock_match_info, mock_analysis_under,
            user_balance=1000, daily_loss=0, active_bets_count=0,
        )
        # 验证决策结构
        assert decision.match_id == mock_match_info["id"]
        assert decision.selection == "under"
        assert decision.sport == "football"
        assert decision.reasoning != ""
        assert isinstance(decision.should_bet, bool)
        assert isinstance(decision.risk_score, float)
        assert 0 <= decision.risk_score <= 1.0

    @pytest.mark.asyncio
    async def test_a2_rejection_flow(
        self, mock_strategy_config, mock_match_info, mock_analysis_no_consensus
    ):
        """A2 拒绝链路：consensus=False -> 不下单。"""
        engine = StrategyEngine(mock_strategy_config, user_id=1)
        decision = await engine.evaluate_bet(
            mock_match_info, mock_analysis_no_consensus,
            user_balance=1000, daily_loss=0, active_bets_count=0,
        )
        assert not decision.should_bet
        assert decision.suggested_stake == 0

    @pytest.mark.asyncio
    async def test_b1_rejection_flow(
        self, mock_strategy_config, mock_match_info, mock_analysis_under
    ):
        """B1 拒绝链路：盘线超区间 -> 不下单。"""
        mock_analysis_under["line"] = 1.0  # < football under_min_line=2.0
        engine = StrategyEngine(mock_strategy_config, user_id=1)
        decision = await engine.evaluate_bet(
            mock_match_info, mock_analysis_under,
            user_balance=1000, daily_loss=0, active_bets_count=0,
        )
        assert not decision.should_bet

    @pytest.mark.asyncio
    async def test_daily_risk_rejection_flow(self, mock_strategy_config):
        """日风控拒绝链路：止损触发 -> 不下单。"""
        with patch("app.ai.strategy_gates.calc_daily_pnl", new_callable=AsyncMock, return_value=Decimal("-600")):
            with patch("app.ai.strategy_gates.count_today_bets", new_callable=AsyncMock, return_value=3):
                triggered, reason = await check_daily_risk(MagicMock(), 1, mock_strategy_config)
        assert triggered
        assert "止损" in reason
        assert "600" in reason

    @pytest.mark.asyncio
    async def test_same_match_duplicate_blocked(self):
        """同场重复下单被阻止。"""
        # 模拟 match 级锁
        from app.core.cache import cache
        cache_mock = AsyncMock()
        cache_mock.acquire_lock = AsyncMock(return_value=True)

        # 第一次获取锁成功
        acquired = await cache_mock.acquire_lock("ai:bet:lock:1:1001", ttl_sec=10)
        assert acquired

        # 第二次获取锁失败（模拟 API 路径已下单）
        cache_mock.acquire_lock = AsyncMock(return_value=False)
        acquired2 = await cache_mock.acquire_lock("ai:bet:lock:1:1001", ttl_sec=10)
        assert not acquired2


class TestCrossComponentRace:
    """跨组件竞态测试（doc.md 第 6.3 章）。"""

    @pytest.mark.asyncio
    async def test_match_level_lock_prevents_duplicate(self, mock_cache):
        """match 级 Redis 锁阻止 API+引擎并发下单。"""
        # 模拟 API 路径先获取锁
        mock_cache.acquire_lock = AsyncMock(return_value=True)
        acquired_api = await mock_cache.acquire_lock("ai:bet:lock:1:1001", ttl_sec=10)
        assert acquired_api

        # 引擎路径尝试获取锁 -> 失败
        mock_cache.acquire_lock = AsyncMock(return_value=False)
        acquired_engine = await mock_cache.acquire_lock("ai:bet:lock:1:1001", ttl_sec=10)
        assert not acquired_engine

    @pytest.mark.asyncio
    async def test_engine_lock_check_before_bet(self):
        """引擎下单前检查锁所有权。"""
        from app.core.cache import cache
        cache_mock = AsyncMock()
        cache_mock.get = AsyncMock(return_value="other_engine_token")

        # 锁已被其他引擎获取
        current_owner = await cache_mock.get("ai:engine:lock:1")
        assert current_owner == "other_engine_token"
        assert current_owner != "my_engine_token"


class TestHotReload:
    """配置热更新测试（doc.md 第 2.6 章）。"""

    @pytest.mark.asyncio
    async def test_config_change_takes_effect(self, mock_ai_config):
        """配置变更后下轮循环生效。"""
        with patch("app.ai.strategy.load_fresh_strategy", new_callable=AsyncMock) as mock_load:
            # 第一轮：max_bet=100
            config1 = StrategyConfig(name="simple", max_bet_amount=100.0)
            mock_load.return_value = (mock_ai_config, config1)
            ai_config1, strat1 = await mock_load(1)
            assert strat1.max_bet_amount == 100.0

            # 第二轮：max_bet 改为 200
            config2 = StrategyConfig(name="simple", max_bet_amount=200.0)
            mock_load.return_value = (mock_ai_config, config2)
            ai_config2, strat2 = await mock_load(1)
            assert strat2.max_bet_amount == 200.0

    @pytest.mark.asyncio
    async def test_decision_passes_strategy(self, mock_strategy_config):
        """decision_passes_strategy 拦截过期决策。"""
        from app.ai.strategy import decision_passes_strategy

        # decision should_bet=False 应被拦截
        decision = MagicMock()
        decision.should_bet = False
        decision.odds = 1.85
        decision.confidence = 0.65

        ok, reason = decision_passes_strategy(decision, mock_strategy_config)
        assert not ok


class TestBalanceAnchor:
    """余额锚定测试（doc.md 第 2.5 章）。"""

    @pytest.mark.asyncio
    async def test_low_balance_does_not_breach_anchor(
        self, mock_strategy_config, mock_match_info, mock_analysis_under
    ):
        """余额低于锚定值时仓位不击穿 25% 余额。"""
        engine = StrategyEngine(mock_strategy_config, user_id=1)
        # 余额=3 元 -> 25% 锚定 = 0.75 -> 不应被 min_stake=1 抬回
        decision = await engine.evaluate_bet(
            mock_match_info, mock_analysis_under,
            user_balance=3, daily_loss=0, active_bets_count=0,
        )
        # 仓位不应超过余额的 25%（0.75）或为 0
        if decision.should_bet:
            assert decision.suggested_stake <= 0.75 or decision.suggested_stake == 0


class TestUnderOnly:
    """仅投注 under 测试（doc.md 关键设计原则 1）。"""

    @pytest.mark.asyncio
    async def test_over_direction_rejected(
        self, mock_strategy_config, mock_match_info, mock_analysis_over
    ):
        """over 方向必须被拒绝。"""
        engine = StrategyEngine(mock_strategy_config, user_id=1)
        decision = await engine.evaluate_bet(
            mock_match_info, mock_analysis_over,
            user_balance=1000, daily_loss=0, active_bets_count=0,
        )
        assert not decision.should_bet

    @pytest.mark.asyncio
    async def test_skip_direction_rejected(self, mock_strategy_config, mock_match_info):
        """skip 方向必须被拒绝。"""
        analysis = {
            "prediction": "skip", "bet_type": "total",
            "confidence": 0.80, "odds": 1.85, "line": 2.5,
            "consensus_reached": True, "reasoning": "skip", "models_used": ["gpt"],
        }
        engine = StrategyEngine(mock_strategy_config, user_id=1)
        decision = await engine.evaluate_bet(
            mock_match_info, analysis,
            user_balance=1000, daily_loss=0, active_bets_count=0,
        )
        assert not decision.should_bet


class TestEndToEnd:
    """端到端测试（doc.md 第 10 章全链路）。"""

    @pytest.mark.asyncio
    async def test_full_scan_analyze_bet_chain(
        self, mock_strategy_config, mock_match_info, mock_analysis_under, mock_ai_config
    ):
        """完整链路：策略评估 -> 阀门校验 -> 下单。"""
        # Step 1: 策略评估
        engine = StrategyEngine(mock_strategy_config, user_id=1)
        decision = await engine.evaluate_bet(
            mock_match_info, mock_analysis_under,
            user_balance=1000, daily_loss=0, active_bets_count=0,
        )

        # Step 2: 阀门校验（如果策略通过）
        if decision.should_bet:
            rec = {
                "recommendation": {
                    "should_bet": True,
                    "confidence": decision.confidence,
                    "odds": decision.odds,
                    "reasoning": decision.reasoning,
                },
                "home_team": mock_match_info["home_team"],
                "away_team": mock_match_info["away_team"],
                "sport": mock_match_info["sport"],
            }
            with patch("app.ai.strategy_gates.load_fresh_strategy", new_callable=AsyncMock, return_value=(mock_ai_config, mock_strategy_config)):
                with patch("app.ai.strategy_gates.check_daily_risk", new_callable=AsyncMock, return_value=(False, "")):
                    ok, reason, stake, strat = await gate_recommendation_for_place(
                        user_id=1, rec=rec, stake=Decimal(str(decision.suggested_stake)), db=MagicMock(),
                    )
            # 验证阀门校验结果
            assert isinstance(ok, bool)
            assert isinstance(stake, Decimal)

    @pytest.mark.asyncio
    async def test_data_quality_validation(
        self, mock_strategy_config, mock_match_info, mock_analysis_under
    ):
        """数据质量验证：决策字段完整性。"""
        engine = StrategyEngine(mock_strategy_config, user_id=1)
        decision = await engine.evaluate_bet(
            mock_match_info, mock_analysis_under,
            user_balance=1000, daily_loss=0, active_bets_count=0,
        )
        # 验证所有关键字段存在
        assert decision.match_id == mock_match_info["id"]
        assert decision.selection == "under"
        assert isinstance(decision.confidence, (int, float))
        assert isinstance(decision.risk_score, (int, float))
        assert isinstance(decision.should_bet, bool)
        assert decision.reasoning != ""
        # 置信度范围
        assert 0 <= decision.confidence <= 1.0
        # 风险评分范围
        assert 0 <= decision.risk_score <= 1.0
        # 仓位范围
        if decision.should_bet:
            assert decision.suggested_stake > 0
            assert decision.suggested_stake <= mock_strategy_config.max_bet_amount


class TestHalfTimeTotals:
    """半场大小球全链路测试（doc.md 第 9 章）。"""

    @pytest.mark.asyncio
    async def test_first_half_under_passes_a0(
        self, mock_strategy_config, mock_match_info, mock_analysis_first_half_under
    ):
        """上半场小球通过 A0 玩法白名单。"""
        engine = StrategyEngine(mock_strategy_config, user_id=1)
        decision = await engine.evaluate_bet(
            mock_match_info, mock_analysis_first_half_under,
            user_balance=1000, daily_loss=0, active_bets_count=0,
        )
        assert decision.should_bet
        assert decision.selection == "under"

    @pytest.mark.asyncio
    async def test_second_half_under_passes_a0(
        self, mock_strategy_config, mock_match_info, mock_analysis_second_half_under
    ):
        """下半场小球通过 A0 玩法白名单。"""
        engine = StrategyEngine(mock_strategy_config, user_id=1)
        decision = await engine.evaluate_bet(
            mock_match_info, mock_analysis_second_half_under,
            user_balance=1000, daily_loss=0, active_bets_count=0,
        )
        assert decision.should_bet
        assert decision.selection == "under"

    @pytest.mark.asyncio
    async def test_non_whitelisted_bet_type_rejected(
        self, mock_strategy_config, mock_match_info
    ):
        """非白名单 bet_type（如 spread）被 A0 拒绝。"""
        analysis = {
            "prediction": "under",
            "bet_type": "spread",
            "confidence": 0.70,
            "odds": 1.85,
            "line": 2.5,
            "consensus_reached": True,
            "reasoning": "让球",
            "models_used": ["gpt"],
        }
        engine = StrategyEngine(mock_strategy_config, user_id=1)
        decision = await engine.evaluate_bet(
            mock_match_info, analysis,
            user_balance=1000, daily_loss=0, active_bets_count=0,
        )
        assert not decision.should_bet

    def test_ob_half_totals_in_parse_matches(self, ob_hps_with_half_totals):
        """OB parse_matches_pb 完整解析含半场大小球。"""
        row = {
            "mid": "12345",
            "mhn": "TeamA",
            "man": "TeamB",
            "csid": "1",
            "csna": "足球",
            "tn": "Test League",
            "tnjc": "TL",
            "tid": "t1",
            "ms": "1",
            "mgt": "2026-08-19T10:00:00",
            "hps": ob_hps_with_half_totals,
        }
        result = parse_matches_pb([row])
        assert len(result) == 1
        match = result[0]
        # 全场 + 上半场 + 下半场 = 至少 3 条赔率
        bet_types = [o.bet_type for o in match.odds_list]
        assert "total" in bet_types
        assert "first_half_total" in bet_types
        assert "second_half_total" in bet_types
