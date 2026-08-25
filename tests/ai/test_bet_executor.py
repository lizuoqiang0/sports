"""统一下单执行器单元测试。

覆盖 bet_executor.py 的核心逻辑：
  - 跨站比价 get_best_market_pack
  - provider_code 四级 fallback 解析
  - 未连接站点自动切换
  - 玩法白名单 / 虚拟赛事 / 中国赛事拦截
  - 仓位异常 / 余额不足 / 赔率无效拦截
  - 下单成功落库 + 通知
  - 下单失败重试 + 通知
  - OB orderNo 待定标记
"""
import sys
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from decimal import Decimal

# 预注入 mock 模块，避免 redis / websockets 等未安装时 app.core.cache 导入失败
for _mod in ("redis", "redis.asyncio", "websockets", "websockets.legacy"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

import app.core.cache  # noqa: E402 — 确保模块可被 patch 定位

from app.ai.strategy import BetDecision, StrategyConfig
from app.ai.bet_executor import (
    BetExecResult,
    _place_failure_is_retryable,
    execute_bet,
    get_best_market_pack,
    get_odds_row,
    mark_bet_pending,
)


def test_only_pre_submit_transient_failures_are_retried():
    assert _place_failure_is_retryable("浏览器网关正忙，请稍后")
    assert _place_failure_is_retryable("网络超时")
    assert not _place_failure_is_retryable("row_not_found:tokHit=[]")
    assert not _place_failure_is_retryable("页面已点投注但余额未扣减")
    assert not _place_failure_is_retryable("wrong_slip_blocked")


def test_pinnacle_order_lookup_rejects_stale_or_non_live_odds():
    source = Path("app/ai/bet_executor.py").read_text(encoding="utf-8")
    get_row = source.split("async def get_odds_row", 1)[1].split(
        "async def mark_bet_pending", 1
    )[0]
    assert "timedelta(minutes=5)" in get_row
    assert "Odds.is_live.is_(True)" in get_row
    assert "Odds.valid_from >= fresh_after" in get_row


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
def mock_match():
    m = MagicMock()
    m.id = 1001
    m.sport = MagicMock()
    m.sport.value = "football"
    m.league = "英超"
    m.home_team = "Arsenal"
    m.away_team = "Chelsea"
    m.external_id = "pinnacle:1634071712"
    m.extra_data = {"ids": {"pinnacle": "pinnacle:1634071712", "ob": "ob:998877"}}
    return m


@pytest.fixture
def mock_user():
    u = MagicMock()
    u.id = 1
    u.balance = Decimal("5000")
    return u


@pytest.fixture
def decision_under():
    return BetDecision(
        match_id=1001,
        selection="under",
        confidence=0.65,
        suggested_stake=Decimal("50"),
        reasoning="防守稳固，小球概率高",
        risk_score=0.3,
        should_bet=True,
        bet_type="total",
        provider_code="pinnacle",
        odds=1.85,
        line=2.5,
        sport="football",
    )


def _mk_site_acc(code="pinnacle", balance=Decimal("1000"), connected=True):
    """构造已连接站点账户 mock。"""
    from app.models.user import BookmakerStatus
    acc = MagicMock()
    acc.code = code
    acc.base_url = "https://real.example.com"
    acc.username = "user1"
    acc.password_encrypted = "enc"
    acc.session_token_encrypted = "enc"
    acc.balance = balance
    acc.profile_json = {}
    acc.enabled = True
    acc.status = BookmakerStatus.CONNECTED if connected else BookmakerStatus.DISCONNECTED
    return acc


def _mk_odds_row(provider="平博", total=2.5, under=1.85, over=1.75):
    """构造赔率行 mock。"""
    row = MagicMock()
    row.provider = provider
    row.total = total
    row.odds_data = {"under": under, "over": over, "line": total}
    row.valid_to = None
    return row


def _mk_pack(best_provider="pinnacle", best_odds=1.85, odds_by_provider=None, line=2.5):
    """构造 best_market_pack mock。"""
    if odds_by_provider is None:
        odds_by_provider = {"平博": {"under": 1.85, "over": 1.75}, "OB体育": {"under": 1.80, "over": 1.70}}
    return {
        "odds": {"under": best_odds, "over": 1.75},
        "best_by_selection": {
            "under": {"provider": "平博" if best_provider == "pinnacle" else "OB体育",
                       "provider_code": best_provider, "odds": best_odds},
            "over": {"provider": "平博", "provider_code": "pinnacle", "odds": 1.75},
        },
        "odds_by_provider": odds_by_provider,
        "line": line,
        "bet_type": "total",
    }


def _mk_place_result(ok=True, external_bet_id=None, actual_stake=0, balance_after=0, message=""):
    """构造 connector.place_bet 返回值 mock。"""
    r = MagicMock()
    r.ok = ok
    r.external_bet_id = external_bet_id
    r.actual_stake = actual_stake
    r.balance_after = balance_after
    r.message = message
    return r


# ── get_best_market_pack 测试 ──


class TestGetBestMarketPack:
    """跨站比价：选赔率最高的站点。"""

    @pytest.mark.asyncio
    async def test_returns_highest_odds_provider(self):
        """under 方向平博赔率更高，应选平博。"""
        matrix = {"under": {"平博": 1.85, "OB体育": 1.80}, "over": {"平博": 1.75, "OB体育": 1.70}}
        with patch("app.ai.market_recommend.load_market_matrix", new_callable=AsyncMock) as glm:
            # load_market_matrix 返回 (matrix, spread_line, total_line)
            glm.return_value = (matrix, None, 2.5)
            pack = await get_best_market_pack(MagicMock(), 1001, "total")

        assert pack["best_by_selection"]["under"]["provider"] == "平博"
        assert pack["best_by_selection"]["under"]["odds"] == 1.85
        assert pack["best_by_selection"]["under"]["provider_code"] == "pinnacle"
        assert pack["line"] == 2.5
        assert "平博" in pack["odds_by_provider"]
        assert pack["odds_by_provider"]["平博"]["under"] == 1.85

    @pytest.mark.asyncio
    async def test_empty_matrix(self):
        """空矩阵返回空结构。"""
        with patch("app.ai.market_recommend.load_market_matrix", new_callable=AsyncMock) as glm:
            glm.return_value = ({}, None, None)
            pack = await get_best_market_pack(MagicMock(), 1001, "total")
        assert pack["best_by_selection"] == {}
        assert pack["line"] is None


# ── execute_bet 玩法白名单测试 ──


class TestBetTypeWhitelist:
    """玩法白名单：仅 total 系 + under/over。"""

    @pytest.mark.asyncio
    async def test_reject_moneyline(self, mock_user, mock_match, strat_cfg, decision_under):
        """moneyline 不在白名单。"""
        decision_under.bet_type = "moneyline"
        result = await execute_bet(MagicMock(), mock_user, mock_match, decision_under, strat_cfg)
        assert not result.ok
        assert "不支持" in result.message

    @pytest.mark.asyncio
    async def test_reject_over_when_disabled(self, mock_user, mock_match, strat_cfg, decision_under):
        """AI_ENABLE_OVER=False 时 over 被拒绝。"""
        decision_under.selection = "over"
        with patch("app.ai.bet_executor.settings") as mock_settings:
            mock_settings.AI_ENABLE_OVER = False
            mock_settings.BET_RETRY_COUNT = 0
            mock_settings.BET_RETRY_DELAY = 0
            result = await execute_bet(MagicMock(), mock_user, mock_match, decision_under, strat_cfg)
        assert not result.ok
        assert "不支持" in result.message


# ── execute_bet 虚拟/中国赛事拦截 ──


class TestMatchFilters:
    """虚拟赛事和中国赛事拦截。"""

    @pytest.mark.asyncio
    async def test_virtual_match_rejected(self, mock_user, mock_match, strat_cfg, decision_under):
        """虚拟赛事被拦截。"""
        mock_match.league = "虚拟联赛"
        with patch("app.services.bookmakers.plugins.ob.odds.is_virtual_match", return_value=True):
            result = await execute_bet(MagicMock(), mock_user, mock_match, decision_under, strat_cfg)
        assert not result.ok
        assert "虚拟" in result.message

    @pytest.mark.asyncio
    async def test_china_match_rejected(self, mock_user, mock_match, strat_cfg, decision_under):
        """中国赛事被拦截。"""
        mock_match.league = "中超"
        mock_match.home_team = "北京国安"
        with patch("app.services.bookmakers.plugins.ob.odds.is_virtual_match", return_value=False), \
             patch("app.services.bookmakers.china_match.is_china_match", return_value=True):
            result = await execute_bet(MagicMock(), mock_user, mock_match, decision_under, strat_cfg)
        assert not result.ok
        assert "中国" in result.message


# ── execute_bet provider 解析 + 切站测试 ──


class TestProviderResolution:
    """provider_code 解析与未连接自动切站。"""

    @pytest.mark.asyncio
    async def test_provider_from_decision(self, mock_user, mock_match, strat_cfg, decision_under):
        """decision.provider_code 优先级最高。"""
        decision_under.provider_code = "ob"
        db = MagicMock()
        db.in_transaction = MagicMock(return_value=False)

        with patch("app.ai.bet_executor.get_best_market_pack", new_callable=AsyncMock) as gbp, \
             patch("app.services.bookmakers.plugins.ob.odds.is_virtual_match", return_value=False), \
             patch("app.services.bookmakers.china_match.is_china_match", return_value=False), \
             patch("app.services.bookmakers.registry.is_real_live_account", return_value=True), \
             patch("app.ai.bet_executor.get_odds_row", new_callable=AsyncMock) as gor, \
             patch("app.services.bookmakers.registry.get_connector") as gc, \
             patch("app.core.cache.cache", new=MagicMock()):

            gbp.return_value = _mk_pack(best_provider="ob", best_odds=1.80)
            gor.return_value = _mk_odds_row(provider="OB体育", under=1.80)

            # 已连接站点
            conn_result = MagicMock()
            conn_result.scalars.return_value.all.return_value = [_mk_site_acc("ob")]
            site_result = MagicMock()
            site_result.scalars.return_value.all.return_value = [_mk_site_acc("ob")]
            db.execute = AsyncMock(side_effect=[conn_result, site_result])

            connector = MagicMock()
            connector.place_bet = AsyncMock(return_value=_mk_place_result(ok=False, message="站点拒绝"))
            connector.fetch_balance = AsyncMock(return_value=Decimal("900"))
            gc.return_value = connector

            result = await execute_bet(db, mock_user, mock_match, decision_under, strat_cfg, is_auto=False)

        # 站点拒绝 -> 失败
        assert not result.ok
        assert result.provider_code == "ob"

    @pytest.mark.asyncio
    async def test_auto_switch_when_not_connected(self, mock_user, mock_match, strat_cfg, decision_under):
        """目标站点未连接时自动切站到已连接站点。"""
        decision_under.provider_code = "ob"  # 决策选 OB
        db = MagicMock()
        db.in_transaction = MagicMock(return_value=False)
        db.add = MagicMock()
        db.flush = AsyncMock()

        pack = _mk_pack(best_provider="ob", best_odds=1.80)

        with patch("app.ai.bet_executor.get_best_market_pack", new_callable=AsyncMock, return_value=pack), \
             patch("app.services.bookmakers.plugins.ob.odds.is_virtual_match", return_value=False), \
             patch("app.services.bookmakers.china_match.is_china_match", return_value=False), \
             patch("app.services.bookmakers.registry.is_real_live_account", return_value=True), \
             patch("app.ai.bet_executor.get_odds_row", new_callable=AsyncMock) as gor, \
             patch("app.services.bookmakers.registry.get_connector") as gc, \
             patch("app.core.cache.cache", new=MagicMock()):

            gor.return_value = _mk_odds_row(provider="平博", under=1.85)

            # 第一次 execute: 查已连接站点 -> 只有 pinnacle
            conn_result = MagicMock()
            conn_result.scalars.return_value.all.return_value = [_mk_site_acc("pinnacle")]
            # 第二次 execute: 查选中站点账户 -> pinnacle
            site_result = MagicMock()
            site_result.scalars.return_value.all.return_value = [_mk_site_acc("pinnacle")]
            db.execute = AsyncMock(side_effect=[conn_result, site_result])

            connector = MagicMock()
            connector.place_bet = AsyncMock(return_value=_mk_place_result(
                ok=True, external_bet_id="p123", actual_stake=50, balance_after=950
            ))
            connector.fetch_balance = AsyncMock(return_value=Decimal("950"))
            gc.return_value = connector

            result = await execute_bet(db, mock_user, mock_match, decision_under, strat_cfg, is_auto=False)

        # 应切站到 pinnacle
        assert result.ok
        assert result.provider_code == "pinnacle"
        assert result.external_bet_id == "p123"

    @pytest.mark.asyncio
    async def test_no_switch_available(self, mock_user, mock_match, strat_cfg, decision_under):
        """目标站点未连接且无替代站点 -> 失败。"""
        decision_under.provider_code = "ob"
        db = MagicMock()

        pack = _mk_pack(best_provider="ob", best_odds=1.80,
                        odds_by_provider={"OB体育": {"under": 1.80}})

        with patch("app.ai.bet_executor.get_best_market_pack", new_callable=AsyncMock, return_value=pack), \
             patch("app.services.bookmakers.plugins.ob.odds.is_virtual_match", return_value=False), \
             patch("app.services.bookmakers.china_match.is_china_match", return_value=False), \
             patch("app.services.bookmakers.registry.is_real_live_account", return_value=True), \
             patch("app.core.cache.cache", new=MagicMock()):

            # 查已连接站点 -> 空（无已连接站点）
            conn_result = MagicMock()
            conn_result.scalars.return_value.all.return_value = []
            db.execute = AsyncMock(return_value=conn_result)

            result = await execute_bet(db, mock_user, mock_match, decision_under, strat_cfg, is_auto=False)

        assert not result.ok
        assert "连接" in result.message


# ── execute_bet 仓位/余额/赔率校验 ──


class TestStakeAndOddsValidation:
    """仓位异常、余额不足、赔率无效。"""

    @pytest.mark.asyncio
    async def test_stake_too_small(self, mock_user, mock_match, strat_cfg, decision_under):
        """仓位 < 1.00 被拒绝。"""
        decision_under.suggested_stake = Decimal("0.50")
        db = MagicMock()
        db.in_transaction = MagicMock(return_value=False)

        with patch("app.ai.bet_executor.get_best_market_pack", new_callable=AsyncMock) as gbp, \
             patch("app.services.bookmakers.plugins.ob.odds.is_virtual_match", return_value=False), \
             patch("app.services.bookmakers.china_match.is_china_match", return_value=False), \
             patch("app.services.bookmakers.registry.is_real_live_account", return_value=True), \
             patch("app.ai.bet_executor.get_odds_row", new_callable=AsyncMock) as gor, \
             patch("app.core.cache.cache", new=MagicMock()):

            gbp.return_value = _mk_pack()
            gor.return_value = _mk_odds_row()

            conn_result = MagicMock()
            conn_result.scalars.return_value.all.return_value = [_mk_site_acc("pinnacle")]
            site_result = MagicMock()
            site_result.scalars.return_value.all.return_value = [_mk_site_acc("pinnacle")]
            db.execute = AsyncMock(side_effect=[conn_result, site_result])

            result = await execute_bet(db, mock_user, mock_match, decision_under, strat_cfg, is_auto=False)

        assert not result.ok
        assert "仓位" in result.message

    @pytest.mark.asyncio
    async def test_insufficient_balance(self, mock_user, mock_match, strat_cfg, decision_under):
        """站点余额不足被拒绝。"""
        decision_under.suggested_stake = Decimal("200")
        db = MagicMock()
        db.in_transaction = MagicMock(return_value=False)

        with patch("app.ai.bet_executor.get_best_market_pack", new_callable=AsyncMock) as gbp, \
             patch("app.services.bookmakers.plugins.ob.odds.is_virtual_match", return_value=False), \
             patch("app.services.bookmakers.china_match.is_china_match", return_value=False), \
             patch("app.services.bookmakers.registry.is_real_live_account", return_value=True), \
             patch("app.ai.bet_executor.get_odds_row", new_callable=AsyncMock) as gor, \
             patch("app.core.cache.cache", new=MagicMock()):

            gbp.return_value = _mk_pack()
            gor.return_value = _mk_odds_row()

            conn_result = MagicMock()
            conn_result.scalars.return_value.all.return_value = [_mk_site_acc("pinnacle")]
            site_result = MagicMock()
            site_result.scalars.return_value.all.return_value = [_mk_site_acc("pinnacle", balance=Decimal("50"))]
            db.execute = AsyncMock(side_effect=[conn_result, site_result])

            result = await execute_bet(db, mock_user, mock_match, decision_under, strat_cfg, is_auto=False)

        assert not result.ok
        assert "余额" in result.message

    @pytest.mark.asyncio
    async def test_zero_odds_rejected(self, mock_user, mock_match, strat_cfg, decision_under):
        """赔率为 0 被拒绝。"""
        decision_under.odds = 0.0
        db = MagicMock()
        db.in_transaction = MagicMock(return_value=False)

        with patch("app.ai.bet_executor.get_best_market_pack", new_callable=AsyncMock) as gbp, \
             patch("app.services.bookmakers.plugins.ob.odds.is_virtual_match", return_value=False), \
             patch("app.services.bookmakers.china_match.is_china_match", return_value=False), \
             patch("app.services.bookmakers.registry.is_real_live_account", return_value=True), \
             patch("app.ai.bet_executor.get_odds_row", new_callable=AsyncMock) as gor, \
             patch("app.core.cache.cache", new=MagicMock()):

            gbp.return_value = _mk_pack(best_odds=0)
            gor.return_value = _mk_odds_row(under=0)

            conn_result = MagicMock()
            conn_result.scalars.return_value.all.return_value = [_mk_site_acc("pinnacle")]
            db.execute = AsyncMock(return_value=conn_result)

            result = await execute_bet(db, mock_user, mock_match, decision_under, strat_cfg, is_auto=False)

        assert not result.ok
        assert "赔率" in result.message


# ── execute_bet 下单成功 ──


class TestSuccessfulBet:
    """下单成功：落库 + 通知 + 余额更新。"""

    @pytest.mark.asyncio
    async def test_pinnacle_success(self, mock_user, mock_match, strat_cfg, decision_under):
        """平博下单成功，返回正确结果。"""
        db = MagicMock()
        db.in_transaction = MagicMock(return_value=False)
        db.add = MagicMock()
        async def _flush_set_id(*a, **kw):
            if db.add.call_args:
                db.add.call_args[0][0].id = 999
        db.flush = AsyncMock(side_effect=_flush_set_id)

        with patch("app.ai.bet_executor.get_best_market_pack", new_callable=AsyncMock) as gbp, \
             patch("app.services.bookmakers.plugins.ob.odds.is_virtual_match", return_value=False), \
             patch("app.services.bookmakers.china_match.is_china_match", return_value=False), \
             patch("app.services.bookmakers.registry.is_real_live_account", return_value=True), \
             patch("app.ai.bet_executor.get_odds_row", new_callable=AsyncMock) as gor, \
             patch("app.services.bookmakers.registry.get_connector") as gc, \
             patch("app.ai.bet_executor._notify", new_callable=AsyncMock) as notify, \
             patch("app.core.cache.cache", new=MagicMock()), \
             patch("app.services.bookmakers.live_poller.pause_live_poller", create=True), \
             patch("app.services.bookmakers.live_poller.resume_live_poller", create=True):

            gbp.return_value = _mk_pack()
            gor.return_value = _mk_odds_row()

            conn_result = MagicMock()
            conn_result.scalars.return_value.all.return_value = [_mk_site_acc("pinnacle")]
            site_result = MagicMock()
            site_result.scalars.return_value.all.return_value = [_mk_site_acc("pinnacle")]
            db.execute = AsyncMock(side_effect=[conn_result, site_result])

            connector = MagicMock()
            connector.place_bet = AsyncMock(return_value=_mk_place_result(
                ok=True, external_bet_id="p123", actual_stake=50, balance_after=950
            ))
            connector.fetch_balance = AsyncMock(return_value=Decimal("950"))
            gc.return_value = connector

            result = await execute_bet(db, mock_user, mock_match, decision_under, strat_cfg, is_auto=True)

        assert result.ok
        assert result.provider_code == "pinnacle"
        assert result.provider_label == "平博"
        assert result.external_bet_id == "p123"
        assert result.stake == Decimal("50")
        assert result.odds == 1.85
        assert result.line == 2.5
        assert result.bet_id is not None
        # 通知已发送
        notify.assert_called_once()
        assert notify.call_args[0][1] == "bet_placed"

    @pytest.mark.asyncio
    async def test_ob_success_marks_pending(self, mock_user, mock_match, strat_cfg, decision_under):
        """OB 下单成功后标记 pending。"""
        decision_under.provider_code = "ob"
        mock_match.external_id = "ob:998877"
        mock_match.extra_data = {"ids": {"ob": "ob:998877"}}

        db = MagicMock()
        db.in_transaction = MagicMock(return_value=False)
        db.add = MagicMock()
        db.flush = AsyncMock()

        with patch("app.ai.bet_executor.get_best_market_pack", new_callable=AsyncMock) as gbp, \
             patch("app.services.bookmakers.plugins.ob.odds.is_virtual_match", return_value=False), \
             patch("app.services.bookmakers.china_match.is_china_match", return_value=False), \
             patch("app.services.bookmakers.registry.is_real_live_account", return_value=True), \
             patch("app.ai.bet_executor.get_odds_row", new_callable=AsyncMock) as gor, \
             patch("app.services.bookmakers.registry.get_connector") as gc, \
             patch("app.ai.bet_executor._notify", new_callable=AsyncMock), \
             patch("app.ai.bet_executor.mark_bet_pending", new_callable=AsyncMock) as mbp, \
             patch("app.core.cache.cache", new=MagicMock()), \
             patch("app.services.bookmakers.live_poller.pause_live_poller", create=True), \
             patch("app.services.bookmakers.live_poller.resume_live_poller", create=True):

            gbp.return_value = _mk_pack(best_provider="ob", best_odds=1.80)
            gor.return_value = _mk_odds_row(provider="OB体育", under=1.80)

            conn_result = MagicMock()
            conn_result.scalars.return_value.all.return_value = [_mk_site_acc("ob")]
            site_result = MagicMock()
            site_result.scalars.return_value.all.return_value = [_mk_site_acc("ob")]
            db.execute = AsyncMock(side_effect=[conn_result, site_result])

            connector = MagicMock()
            connector.place_bet = AsyncMock(return_value=_mk_place_result(
                ok=True, external_bet_id="ob_order_001", actual_stake=50, balance_after=950
            ))
            connector.fetch_balance = AsyncMock(return_value=Decimal("950"))
            gc.return_value = connector

            result = await execute_bet(db, mock_user, mock_match, decision_under, strat_cfg, is_auto=False)

        assert result.ok
        assert result.provider_code == "ob"
        assert result.external_bet_id == "ob_order_001"
        # OB 下单后标记 pending
        mbp.assert_called_once()
        assert mbp.call_args[0][2] == "ob_order_001"  # order_no is 3rd positional arg


# ── execute_bet 下单失败 + 重试 ──


class TestBetFailure:
    """下单失败：重试 + 通知。"""

    @pytest.mark.asyncio
    async def test_retry_then_success(self, mock_user, mock_match, strat_cfg, decision_under):
        """第一次失败、重试后成功。"""
        db = MagicMock()
        db.in_transaction = MagicMock(return_value=False)
        db.add = MagicMock()
        db.flush = AsyncMock()

        with patch("app.ai.bet_executor.get_best_market_pack", new_callable=AsyncMock) as gbp, \
             patch("app.services.bookmakers.plugins.ob.odds.is_virtual_match", return_value=False), \
             patch("app.services.bookmakers.china_match.is_china_match", return_value=False), \
             patch("app.services.bookmakers.registry.is_real_live_account", return_value=True), \
             patch("app.ai.bet_executor.get_odds_row", new_callable=AsyncMock) as gor, \
             patch("app.services.bookmakers.registry.get_connector") as gc, \
             patch("app.ai.bet_executor._notify", new_callable=AsyncMock), \
             patch("app.core.cache.cache", new=MagicMock()), \
             patch("app.services.bookmakers.live_poller.pause_live_poller", create=True), \
             patch("app.services.bookmakers.live_poller.resume_live_poller", create=True), \
             patch("app.ai.bet_executor.settings") as mock_settings:

            mock_settings.AI_ENABLE_OVER = False
            mock_settings.BET_RETRY_COUNT = 1
            mock_settings.BET_RETRY_DELAY = 0

            gbp.return_value = _mk_pack()
            gor.return_value = _mk_odds_row()

            conn_result = MagicMock()
            conn_result.scalars.return_value.all.return_value = [_mk_site_acc("pinnacle")]
            site_result = MagicMock()
            site_result.scalars.return_value.all.return_value = [_mk_site_acc("pinnacle")]
            db.execute = AsyncMock(side_effect=[conn_result, site_result])

            connector = MagicMock()
            # 第一次失败，第二次成功
            connector.place_bet = AsyncMock(side_effect=[
                _mk_place_result(ok=False, message="超时"),
                _mk_place_result(ok=True, external_bet_id="p456", actual_stake=50, balance_after=950),
            ])
            connector.fetch_balance = AsyncMock(return_value=Decimal("950"))
            gc.return_value = connector

            result = await execute_bet(db, mock_user, mock_match, decision_under, strat_cfg, is_auto=True)

        assert result.ok
        assert result.external_bet_id == "p456"
        assert connector.place_bet.call_count == 2

    @pytest.mark.asyncio
    async def test_all_retries_exhausted(self, mock_user, mock_match, strat_cfg, decision_under):
        """维护/DOM 等非瞬时失败不盲目重复下单。"""
        db = MagicMock()
        db.in_transaction = MagicMock(return_value=False)

        with patch("app.ai.bet_executor.get_best_market_pack", new_callable=AsyncMock) as gbp, \
             patch("app.services.bookmakers.plugins.ob.odds.is_virtual_match", return_value=False), \
             patch("app.services.bookmakers.china_match.is_china_match", return_value=False), \
             patch("app.services.bookmakers.registry.is_real_live_account", return_value=True), \
             patch("app.ai.bet_executor.get_odds_row", new_callable=AsyncMock) as gor, \
             patch("app.services.bookmakers.registry.get_connector") as gc, \
             patch("app.ai.bet_executor._notify", new_callable=AsyncMock) as notify, \
             patch("app.core.cache.cache", new=MagicMock()), \
             patch("app.services.bookmakers.live_poller.pause_live_poller", create=True), \
             patch("app.services.bookmakers.live_poller.resume_live_poller", create=True), \
             patch("app.ai.bet_executor.settings") as mock_settings:

            mock_settings.AI_ENABLE_OVER = False
            mock_settings.BET_RETRY_COUNT = 1
            mock_settings.BET_RETRY_DELAY = 0

            gbp.return_value = _mk_pack()
            gor.return_value = _mk_odds_row()

            conn_result = MagicMock()
            conn_result.scalars.return_value.all.return_value = [_mk_site_acc("pinnacle")]
            site_result = MagicMock()
            site_result.scalars.return_value.all.return_value = [_mk_site_acc("pinnacle")]
            db.execute = AsyncMock(side_effect=[conn_result, site_result])

            connector = MagicMock()
            connector.place_bet = AsyncMock(return_value=_mk_place_result(ok=False, message="站点维护中"))
            connector.fetch_balance = AsyncMock(return_value=Decimal("1000"))
            gc.return_value = connector

            result = await execute_bet(db, mock_user, mock_match, decision_under, strat_cfg, is_auto=False)

        assert not result.ok
        assert "站点维护中" in result.message
        assert connector.place_bet.call_count == 1
        # 失败通知已发送
        notify.assert_called_once()
        assert notify.call_args[0][1] == "bet_failed"


# ── provider_code fallback 测试 ──


class TestProviderFallback:
    """provider_code 四级 fallback：决策 > 最优赔率 > external_id > 兜底。"""

    @pytest.mark.asyncio
    async def test_fallback_to_external_id(self, mock_user, mock_match, strat_cfg, decision_under):
        """决策无 provider_code、pack 也无 -> 从 external_id 推断。"""
        decision_under.provider_code = ""
        mock_match.external_id = "ob:998877"
        mock_match.extra_data = {"ids": {"ob": "ob:998877"}}
        db = MagicMock()
        db.in_transaction = MagicMock(return_value=False)
        db.add = MagicMock()
        db.flush = AsyncMock()

        with patch("app.ai.bet_executor.get_best_market_pack", new_callable=AsyncMock) as gbp, \
             patch("app.services.bookmakers.plugins.ob.odds.is_virtual_match", return_value=False), \
             patch("app.services.bookmakers.china_match.is_china_match", return_value=False), \
             patch("app.services.bookmakers.registry.is_real_live_account", return_value=True), \
             patch("app.ai.bet_executor.get_odds_row", new_callable=AsyncMock) as gor, \
             patch("app.services.bookmakers.registry.get_connector") as gc, \
             patch("app.ai.bet_executor._notify", new_callable=AsyncMock), \
             patch("app.core.cache.cache", new=MagicMock()), \
             patch("app.services.bookmakers.live_poller.pause_live_poller", create=True), \
             patch("app.services.bookmakers.live_poller.resume_live_poller", create=True):

            # pack 无 best_by_selection for under
            gbp.return_value = _mk_pack(best_provider="", best_odds=0)
            # 清空 best_by_selection
            gbp.return_value["best_by_selection"] = {}
            gor.return_value = _mk_odds_row(provider="OB体育", under=1.80)

            conn_result = MagicMock()
            conn_result.scalars.return_value.all.return_value = [_mk_site_acc("ob")]
            site_result = MagicMock()
            site_result.scalars.return_value.all.return_value = [_mk_site_acc("ob")]
            db.execute = AsyncMock(side_effect=[conn_result, site_result])

            connector = MagicMock()
            connector.place_bet = AsyncMock(return_value=_mk_place_result(
                ok=True, external_bet_id="ob_ext_001", actual_stake=50, balance_after=950
            ))
            connector.fetch_balance = AsyncMock(return_value=Decimal("950"))
            gc.return_value = connector

            result = await execute_bet(db, mock_user, mock_match, decision_under, strat_cfg, is_auto=False)

        assert result.ok
        assert result.provider_code == "ob"  # 从 external_id "ob:" 推断

    @pytest.mark.asyncio
    async def test_fallback_to_default_pinnacle(self, mock_user, mock_match, strat_cfg, decision_under):
        """无决策、无 pack、无 external_id 前缀 -> 兜底 pinnacle。"""
        decision_under.provider_code = ""
        mock_match.external_id = "unknown:12345"
        mock_match.extra_data = {"ids": {"pinnacle": "pinnacle:default_id"}}
        db = MagicMock()
        db.in_transaction = MagicMock(return_value=False)
        db.add = MagicMock()
        async def _flush_set_id(*a, **kw):
            if db.add.call_args:
                db.add.call_args[0][0].id = 999
        db.flush = AsyncMock(side_effect=_flush_set_id)

        with patch("app.ai.bet_executor.get_best_market_pack", new_callable=AsyncMock) as gbp, \
             patch("app.services.bookmakers.plugins.ob.odds.is_virtual_match", return_value=False), \
             patch("app.services.bookmakers.china_match.is_china_match", return_value=False), \
             patch("app.services.bookmakers.registry.is_real_live_account", return_value=True), \
             patch("app.ai.bet_executor.get_odds_row", new_callable=AsyncMock) as gor, \
             patch("app.services.bookmakers.registry.get_connector") as gc, \
             patch("app.ai.bet_executor._notify", new_callable=AsyncMock), \
             patch("app.core.cache.cache", new=MagicMock()), \
             patch("app.services.bookmakers.live_poller.pause_live_poller", create=True), \
             patch("app.services.bookmakers.live_poller.resume_live_poller", create=True):

            gbp.return_value = _mk_pack()
            gbp.return_value["best_by_selection"] = {}
            gor.return_value = _mk_odds_row()

            conn_result = MagicMock()
            conn_result.scalars.return_value.all.return_value = [_mk_site_acc("pinnacle")]
            site_result = MagicMock()
            site_result.scalars.return_value.all.return_value = [_mk_site_acc("pinnacle")]
            db.execute = AsyncMock(side_effect=[conn_result, site_result])

            connector = MagicMock()
            connector.place_bet = AsyncMock(return_value=_mk_place_result(
                ok=True, external_bet_id="p_default", actual_stake=50, balance_after=950
            ))
            connector.fetch_balance = AsyncMock(return_value=Decimal("950"))
            gc.return_value = connector

            result = await execute_bet(db, mock_user, mock_match, decision_under, strat_cfg, is_auto=False)

        assert result.ok
        assert result.provider_code == "pinnacle"  # 兜底


# ── mark_bet_pending 测试 ──


class TestMarkBetPending:
    """OB 待定注单标记。"""

    @pytest.mark.asyncio
    async def test_marks_pending(self):
        """标记 OB orderNo 到 Redis。"""
        with patch("app.core.cache.cache") as mock_cache:
            mock_cache.set_json = AsyncMock(return_value=True)
            await mark_bet_pending(
                user_id=1,
                match_id=1001,
                order_no="ob_order_001",
                selection="under",
                bet_type="total",
                odds=1.85,
                stake=Decimal("50"),
                line=2.5,
                confidence=0.65,
                reasoning="小球概率高",
                provider="OB体育",
            )
        mock_cache.set_json.assert_called_once()
        key = mock_cache.set_json.call_args[0][0]
        assert "ai:bet:pending:1:1001" in key
