"""仓位计算单元测试：小样本连败保护 + 站点降仓因子 + min_stake 兜底交互。

验证 _bucket_factor 修改后的行为：
1. 小样本(n<8) + 连败≥4 → 降仓×0.5
2. 小样本(n<8) + 连败<4 → 不降仓×1.0
3. 大样本(n≥8) 正常降仓路径不受影响
4. 边界连续性：n=7→8 过渡无反转
5. min_stake 兜底对小 max_bet 场景的影响
6. 无 provider_code 时仓位不受站点因子影响
"""
from __future__ import annotations

import pytest
from contextlib import contextmanager
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from app.ai.strategy import StrategyConfig, StrategyEngine


# ═══════════════════════════════════════════════════════════════
# 辅助构造函数
# ═══════════════════════════════════════════════════════════════

_PROVIDER_KEY = "平博"


def _make_engine(max_bet: float = 100.0, min_conf: float = 0.47) -> StrategyEngine:
    """构造测试用策略引擎。"""
    cfg = StrategyConfig(
        name="test",
        max_bet_amount=max_bet,
        min_confidence=min_conf,
        min_odds=1.65,
        max_odds=5.0,
        stop_loss=500.0,
        take_profit=1000.0,
    )
    return StrategyEngine(config=cfg, user_id=1)


def _base_match() -> dict:
    """构造能通过全部 A-E 闸门的足球 under 比赛。

    关键参数选择理由：
    - home/away_score=0 → current_total=0, margin=2.5（>1.0 过 B1b, >1.5 跳过 D1b）
    - total_line=2.5 → 在 [2.0, 5.0] 区间内（过 B1）
    - clock="45'" → played_mins≈45（≥20 过 B1 早段, <90 不过末段, <58.5*65% 不触发 time_bump）
    - odds under=1.80 → <2.0 过 B3, <1.90 跳过 B3b
    - line_movements={} → neutral 过 C1
    - 0-0 且 line=2.5 <3.0 → 不触发 P8
    - pace=0 < 2.5 → 不触发 P5
    """
    return {
        "id": 1001,
        "sport": "football",
        "league": "英超",
        "home_team": "Arsenal",
        "away_team": "Chelsea",
        "home_score": 0,
        "away_score": 0,
        "clock": "45'",
        "period": "1H",
        "total_line": 2.5,
        "odds": {"under": 1.80, "over": 1.90},
        "line_movements": {},
    }


def _base_analysis(provider_code: str = "pinnacle") -> dict:
    """构造能通过全部 A-E 闸门的 under 分析。

    confidence=0.70 > under_min_conf_no_fund=0.68 → 过 A3
    odds=1.80 → 过 E1/E2（breakeven=1/1.80≈0.556 < 0.70）
    """
    return {
        "prediction": "under",
        "bet_type": "total",
        "confidence": 0.70,
        "odds": 1.80,
        "line": 2.5,
        "consensus_reached": True,
        "reasoning": "test",
        "context_source": "none",
        "provider_code": provider_code,
    }


def _provider_stats(
    settled: int = 5,
    won: int = 1,
    streak: int = 4,
    sel_settled: int | None = None,
    sel_won: int | None = None,
    sel_streak: int | None = None,
) -> dict:
    """构造 by_provider[provider] 统计数据。

    Args:
        settled: 站点级结算注数
        won: 站点级赢注数
        streak: 站点级连败数
        sel_settled: 方向级结算注数（默认=站点级）
        sel_won: 方向级赢注数（默认=站点级）
        sel_streak: 方向级连败数（默认=站点级）
    """
    sel_settled = sel_settled if sel_settled is not None else settled
    sel_won = sel_won if sel_won is not None else won
    sel_streak = sel_streak if sel_streak is not None else streak

    lost = settled - won
    sel_lost = sel_settled - sel_won
    stake = float(settled * 50)
    payout = float(won * 50 * 1.80)
    sel_stake = float(sel_settled * 50)
    sel_payout = float(sel_won * 50 * 1.80)

    def _wr(n, w):
        return round(w / n, 4) if n > 0 else None

    def _roi(s, p):
        return round((p - s) / s, 4) if s > 0 else None

    return {
        "settled": settled,
        "won": won,
        "lost": lost,
        "win_rate": _wr(settled, won),
        "stake": stake,
        "payout": payout,
        "roi": _roi(stake, payout),
        "loss_streak": streak,
        "by_selection": {
            "under": {
                "settled": sel_settled,
                "won": sel_won,
                "lost": sel_lost,
                "win_rate": _wr(sel_settled, sel_won),
                "stake": sel_stake,
                "payout": sel_payout,
                "roi": _roi(sel_stake, sel_payout),
                "loss_streak": sel_streak,
            }
        },
    }


def _cached_stats(provider: dict | None = None) -> dict:
    """构造完整的 _cached_stats。

    by_selection.under.settled=3（<5）→ 不触发 A3 胜率自适应 bump
    by_provider 由调用方传入
    """
    stats = {
        "settled": 3,
        "win_rate": 0.5,
        "by_selection": {
            "under": {"settled": 3, "win_rate": 0.5},
        },
        "by_provider": {},
    }
    if provider is not None:
        stats["by_provider"] = {_PROVIDER_KEY: provider}
    return stats


@contextmanager
def _mock_deps():
    """Mock 外部依赖，隔离 DB/Redis/时钟解析。

    - match_elapsed_seconds → 2700秒(45分钟)
    - parse_match_clock_minutes → 45.0
    - calibration 系列 → 空结果（不触发动态 bump / 模式拦截）
    - provider_name → 返回已知 key
    """
    with (
        patch(
            "app.services.bookmakers.match_live.match_elapsed_seconds",
            return_value=2700,
        ),
        patch(
            "app.services.bookmakers.match_live.parse_match_clock_minutes",
            return_value=45.0,
        ),
        patch(
            "app.ai.calibration.load_risk_tuning",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch(
            "app.ai.calibration.get_dynamic_conf_bump",
            return_value=0.0,
        ),
        patch(
            "app.ai.calibration.load_risk_patterns",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "app.ai.calibration.check_risk_patterns",
            return_value=None,
        ),
        patch(
            "app.services.bookmakers.catalog.provider_name",
            return_value=_PROVIDER_KEY,
        ),
    ):
        yield


async def _eval(engine: StrategyEngine, provider_code: str = "pinnacle") -> object:
    """运行 evaluate_bet 并返回 BetDecision。"""
    engine._cached_stats = _cached_stats(engine._test_provider)  # type: ignore
    with _mock_deps():
        decision = await engine.evaluate_bet(
            _base_match(),
            _base_analysis(provider_code=provider_code),
            user_balance=Decimal("1000"),
            daily_loss=Decimal("0"),
            active_bets_count=0,
        )
    return decision


# ═══════════════════════════════════════════════════════════════
# 基准仓位计算（无站点因子时的期望值）
# ═══════════════════════════════════════════════════════════════

def _expected_baseline_stake(max_bet: float = 100.0) -> Decimal:
    """计算无 provider 调整时的期望仓位。

    conf_scale: 0.70>=0.65 → 0.90; under: min(0.90*1.10, 0.95)=0.95
    risk_score: (1-0.70)*0.4 = 0.12 (odds=1.80 不>1.80 无赔率惩罚)
    risk_factor: 1-0.12*0.30 = 0.964
    conf_scale*risk_factor = 0.95*0.964 = 0.9158 → round 0.916
    stake = max_bet * 0.916
    """
    return (Decimal(str(max_bet)) * Decimal("0.916")).quantize(
        Decimal("0.01")
    )


# ═══════════════════════════════════════════════════════════════
# 测试类
# ═══════════════════════════════════════════════════════════════


class TestSmallSampleLosingStreakProtection:
    """小样本连败保护：n<8 但 loss_streak≥4 时降仓×0.5。"""

    @pytest.mark.asyncio
    async def test_streak4_reduces_stake_by_half(self):
        """n=5 streak=4 → 仓位约为基准的50%。"""
        baseline = _expected_baseline_stake(100.0)  # 91.60

        engine = _make_engine(max_bet=100.0)
        engine._test_provider = _provider_stats(settled=5, won=1, streak=4)  # type: ignore
        decision = await _eval(engine)

        assert decision.should_bet, f"应通过全部闸门: {decision.reasoning}"
        actual = decision.suggested_stake
        expected = (baseline * Decimal("0.5")).quantize(Decimal("0.01"))  # 45.80

        assert actual == expected, (
            f"小样本连败保护: 期望 {expected}（基准{baseline}×0.5）, 实际 {actual}"
        )

    @pytest.mark.asyncio
    async def test_streak3_no_reduction(self):
        """n=5 streak=3 → 未达4连败阈值，不降仓。"""
        baseline = _expected_baseline_stake(100.0)  # 91.60

        engine = _make_engine(max_bet=100.0)
        engine._test_provider = _provider_stats(settled=5, won=2, streak=3)  # type: ignore
        decision = await _eval(engine)

        assert decision.should_bet, f"应通过全部闸门: {decision.reasoning}"
        actual = decision.suggested_stake

        assert actual == baseline, (
            f"连败3不应触发降仓: 期望 {baseline}, 实际 {actual}"
        )

    @pytest.mark.asyncio
    async def test_streak0_no_reduction(self):
        """n=5 streak=0 → 不降仓。"""
        baseline = _expected_baseline_stake(100.0)

        engine = _make_engine(max_bet=100.0)
        engine._test_provider = _provider_stats(settled=5, won=3, streak=0)  # type: ignore
        decision = await _eval(engine)

        assert decision.should_bet, f"应通过全部闸门: {decision.reasoning}"
        actual = decision.suggested_stake

        assert actual == baseline, (
            f"无连败不应降仓: 期望 {baseline}, 实际 {actual}"
        )

    @pytest.mark.asyncio
    async def test_streak5_reduces_stake_by_half(self):
        """n=6 streak=5 → 仍降仓×0.5（小样本路径，streak 仅需≥4）。"""
        baseline = _expected_baseline_stake(100.0)

        engine = _make_engine(max_bet=100.0)
        engine._test_provider = _provider_stats(settled=6, won=1, streak=5)  # type: ignore
        decision = await _eval(engine)

        assert decision.should_bet, f"应通过全部闸门: {decision.reasoning}"
        actual = decision.suggested_stake
        expected = (baseline * Decimal("0.5")).quantize(Decimal("0.01"))

        assert actual == expected, (
            f"连败5小样本: 期望 {expected}, 实际 {actual}"
        )


class TestLargeSampleUnaffected:
    """大样本(n≥8)路径不受小样本保护影响，走原有 ROI/streak 逻辑。"""

    @pytest.mark.asyncio
    async def test_large_sample_normal_roi(self):
        """n=10 streak=0 ROI=+10% → 仓位×1.1（盈利加仓）。"""
        baseline = _expected_baseline_stake(100.0)

        # ROI = (9*50*1.80 - 10*50) / (10*50) = (810-500)/500 = 0.62 → >0.10 → f=1.1
        engine = _make_engine(max_bet=100.0)
        engine._test_provider = _provider_stats(settled=10, won=9, streak=0)  # type: ignore
        decision = await _eval(engine)

        assert decision.should_bet, f"应通过全部闸门: {decision.reasoning}"
        actual = decision.suggested_stake
        expected = (baseline * Decimal("1.1")).quantize(Decimal("0.01"))  # 100.76

        # max_bet cap = 100.0
        expected = min(expected, Decimal("100.00"))

        assert actual == expected, (
            f"大样本盈利加仓: 期望 {expected}（基准×1.1 cap 100）, 实际 {actual}"
        )

    @pytest.mark.asyncio
    async def test_large_sample_heavy_loss_capped(self):
        """n=10 streak=6 ROI≤-0.25 → f=0.6×0.35=0.21 → capped 0.3。"""
        baseline = _expected_baseline_stake(100.0)

        # ROI = (1*50*1.80 - 10*50)/(10*50) = (90-500)/500 = -0.82 → ≤-0.25 → f=0.6
        # streak=6 → f*=0.35 → 0.6*0.35=0.21 → max(0.21, 0.3)=0.3
        engine = _make_engine(max_bet=100.0)
        engine._test_provider = _provider_stats(settled=10, won=1, streak=6)  # type: ignore
        decision = await _eval(engine)

        assert decision.should_bet, f"应通过全部闸门: {decision.reasoning}"
        actual = decision.suggested_stake
        expected = (baseline * Decimal("0.3")).quantize(Decimal("0.01"))  # 27.48

        assert actual == expected, (
            f"大样本重亏封底: 期望 {expected}（基准×0.3）, 实际 {actual}"
        )


class TestBoundaryConsistency:
    """边界连续性：n=7→8 过渡无反转。"""

    @pytest.mark.asyncio
    async def test_n7_streak4_factor_0_5(self):
        """n=7 streak=4 → 小样本保护 ×0.5。"""
        baseline = _expected_baseline_stake(100.0)

        engine = _make_engine(max_bet=100.0)
        engine._test_provider = _provider_stats(settled=7, won=3, streak=4)  # type: ignore
        decision = await _eval(engine)

        assert decision.should_bet
        actual = decision.suggested_stake
        expected = (baseline * Decimal("0.5")).quantize(Decimal("0.01"))

        assert actual == expected, (
            f"n=7 streak=4: 期望 {expected}, 实际 {actual}"
        )

    @pytest.mark.asyncio
    async def test_n8_streak4_no_reversal(self):
        """n=8 streak=4 ROI≤-0.15 → f=0.8×0.5=0.4 < 0.5（方向一致，无反转）。

        n=7→n=8: 因子从 0.5 降到 0.4，方向一致（样本越多，对亏损越确信）。
        """
        baseline = _expected_baseline_stake(100.0)

        # ROI = (4*50*1.80 - 8*50)/(8*50) = (360-400)/400 = -0.10 → >-0.15 → f=1.0
        # Hmm, -0.10 is not ≤ -0.15. Let me adjust: won=3 → ROI=(270-400)/400=-0.325 → ≤-0.25 → f=0.6
        # streak=4 → 0.6*0.5=0.3 → max(0.3, 0.3)=0.3
        # That's 0.3 < 0.5, which is consistent direction (more data → more penalty)
        engine = _make_engine(max_bet=100.0)
        engine._test_provider = _provider_stats(settled=8, won=3, streak=4)  # type: ignore
        decision = await _eval(engine)

        assert decision.should_bet
        actual = decision.suggested_stake

        # n=7: factor=0.5, n=8: factor should be ≤0.5 (no reversal)
        n7_stake = (baseline * Decimal("0.5")).quantize(Decimal("0.01"))

        assert actual <= n7_stake, (
            f"边界连续性: n=8 仓位 {actual} 应 ≤ n=7 仓位 {n7_stake}（无反转）"
        )


class TestMinStakeFloor:
    """min_stake 兜底与降仓保护的交互。"""

    @pytest.mark.asyncio
    async def test_small_max_bet_protection_not_eaten_by_floor(self):
        """max_bet=2.0 时降仓保护不再被 min_stake 兜底吞没。

        基准 = 2.0 × 0.916 = 1.83
        带 ×0.5 → 0.92（< 1.0 但 prov_factor<1.0 时跳过 min_stake 兜底）
        保护完全生效：仓位 = 基准 × 0.5 = 0.92
        执行层 bet_executor 会拒绝 < 1.0 的仓位，等效于跳过高风险单
        """
        baseline = _expected_baseline_stake(2.0)  # 1.83

        engine = _make_engine(max_bet=2.0)
        engine._test_provider = _provider_stats(settled=5, won=1, streak=4)  # type: ignore
        decision = await _eval(engine)

        assert decision.should_bet, f"应通过全部闸门: {decision.reasoning}"
        actual = decision.suggested_stake

        # 保护后仓位 < 基准（保护完全生效）
        assert actual < baseline, (
            f"小 max_bet 保护应有效: 实际 {actual} < 基准 {baseline}"
        )
        # 不再被 min_stake 兜底到 1.0，保持降仓后的实际值
        expected = (baseline * Decimal("0.5")).quantize(Decimal("0.01"))
        assert actual == expected, (
            f"降仓保护不被 min_stake 吞没: 期望 {expected}, 实际 {actual}"
        )

    @pytest.mark.asyncio
    async def test_normal_max_bet_protection_full_effect(self):
        """max_bet=100.0 时保护完全生效（不被 min_stake 吞没）。"""
        baseline = _expected_baseline_stake(100.0)  # 91.60

        engine = _make_engine(max_bet=100.0)
        engine._test_provider = _provider_stats(settled=5, won=1, streak=4)  # type: ignore
        decision = await _eval(engine)

        assert decision.should_bet
        actual = decision.suggested_stake
        expected = (baseline * Decimal("0.5")).quantize(Decimal("0.01"))  # 45.80

        assert actual == expected, (
            f"正常 max_bet 保护完全生效: 期望 {expected}, 实际 {actual}"
        )


class TestNoProviderCode:
    """无 provider_code 时仓位不受站点因子影响。"""

    @pytest.mark.asyncio
    async def test_empty_provider_code_no_adjustment(self):
        """provider_code="" → 不触发站点降仓，仓位=基准。"""
        baseline = _expected_baseline_stake(100.0)

        engine = _make_engine(max_bet=100.0)
        engine._test_provider = _provider_stats(settled=5, won=1, streak=4)  # type: ignore
        # provider_code 为空 → 不进入 provider staking 分支
        decision = await _eval(engine, provider_code="")

        assert decision.should_bet, f"应通过全部闸门: {decision.reasoning}"
        actual = decision.suggested_stake

        assert actual == baseline, (
            f"无 provider_code 不应降仓: 期望 {baseline}, 实际 {actual}"
        )

    @pytest.mark.asyncio
    async def test_unknown_provider_code_no_adjustment(self):
        """provider_code="unknown" → 不触发站点降仓。"""
        baseline = _expected_baseline_stake(100.0)

        engine = _make_engine(max_bet=100.0)
        engine._test_provider = _provider_stats(settled=5, won=1, streak=4)  # type: ignore
        decision = await _eval(engine, provider_code="unknown")

        assert decision.should_bet, f"应通过全部闸门: {decision.reasoning}"
        actual = decision.suggested_stake

        assert actual == baseline, (
            f"未知 provider 不应降仓: 期望 {baseline}, 实际 {actual}"
        )


class TestDirectionLevelProtection:
    """方向级（by_selection）小样本连败保护独立生效。"""

    @pytest.mark.asyncio
    async def test_site_ok_direction_triggers(self):
        """站点级 n=10 正常但方向级 n=5 streak=4 → 方向级降仓×0.5。

        f_site=1.0（大样本正常）, f_dir=0.5（小样本连败）
        prov_factor = min(1.0, 0.5) = 0.5
        """
        baseline = _expected_baseline_stake(100.0)

        engine = _make_engine(max_bet=100.0)
        # 站点级：n=10 won=9 streak=0 → f_site=1.1
        # 方向级：n=5 won=1 streak=4 → f_dir=0.5
        engine._test_provider = _provider_stats(  # type: ignore
            settled=10, won=9, streak=0,
            sel_settled=5, sel_won=1, sel_streak=4,
        )
        decision = await _eval(engine)

        assert decision.should_bet, f"应通过全部闸门: {decision.reasoning}"
        actual = decision.suggested_stake
        # min(1.1, 0.5) = 0.5
        expected = (baseline * Decimal("0.5")).quantize(Decimal("0.01"))

        assert actual == expected, (
            f"方向级独立降仓: 期望 {expected}（基准×min(1.1,0.5)=0.5）, 实际 {actual}"
        )

    @pytest.mark.asyncio
    async def test_site_triggers_direction_ok(self):
        """站点级 n=5 streak=4 但方向级 n=10 正常 → 站点级降仓×0.5。

        f_site=0.5（小样本连败）, f_dir=1.1（大样本盈利）
        prov_factor = min(0.5, 1.1) = 0.5
        """
        baseline = _expected_baseline_stake(100.0)

        engine = _make_engine(max_bet=100.0)
        # 站点级：n=5 won=1 streak=4 → f_site=0.5
        # 方向级：n=10 won=9 streak=0 → f_dir=1.1
        engine._test_provider = _provider_stats(  # type: ignore
            settled=5, won=1, streak=4,
            sel_settled=10, sel_won=9, sel_streak=0,
        )
        decision = await _eval(engine)

        assert decision.should_bet, f"应通过全部闸门: {decision.reasoning}"
        actual = decision.suggested_stake
        # min(0.5, 1.1) = 0.5
        expected = (baseline * Decimal("0.5")).quantize(Decimal("0.01"))

        assert actual == expected, (
            f"站点级独立降仓: 期望 {expected}（基准×min(0.5,1.1)=0.5）, 实际 {actual}"
        )
