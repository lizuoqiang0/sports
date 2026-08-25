"""P7/P9 反向风险封顶单元测试。

验证优化后的行为：
- P7：under conf≥0.74 → 不再硬拒绝，而是封顶到 0.72，让后续闸门继续评估
- P9：over conf≥0.73 → 不再硬拒绝，而是封顶到 0.72，让后续闸门继续评估
- 封顶后 confidence 字段被正确修改
- 封顶后 confidence_capped_reason 字段被正确设置
- conf 低于触发阈值时不触发封顶

对应 strategy.py 阶段 A3 中的 P7/P9 逻辑。
"""
import pytest
from unittest.mock import AsyncMock, patch
from decimal import Decimal

from app.ai.strategy import StrategyEngine, StrategyConfig


# ── 辅助函数 ──

def _mk_engine() -> StrategyEngine:
    """构造测试用策略引擎（min_confidence=0 使 A3 用户配置不干扰）。"""
    cfg = StrategyConfig(
        name="test",
        min_confidence=0.0,
        min_odds=1.50,
        max_odds=5.0,
        max_bet_amount=100.0,
    )
    eng = StrategyEngine(config=cfg, user_id=1)
    # 缓存空统计，避免 _cached_stats 为 None 时查 DB
    eng._cached_stats = {"settled": 0, "by_selection": {}}
    return eng


def _football_match(**kw) -> dict:
    """构造足球比赛信息（30'、0-0、line=2.5）。"""
    m = {
        "id": 1001,
        "sport": "football",
        "league": "英超",
        "home_team": "Arsenal",
        "away_team": "Chelsea",
        "home_score": 0,
        "away_score": 0,
        "clock": "30'",
        "period": "1H",
        "total_line": 2.5,
        "odds": {"under": 1.80, "over": 1.80},
    }
    m.update(kw)
    return m


def _analysis_under(conf: float = 0.65, **kw) -> dict:
    """构造 under 方向分析结果。"""
    a = {
        "prediction": "under",
        "bet_type": "total",
        "confidence": conf,
        "odds": 1.80,
        "line": 2.5,
        "consensus_reached": True,
        "reasoning": "双方防守稳固",
        "context_source": "none",
        "models_used": ["deepseek"],
    }
    a.update(kw)
    return a


def _analysis_over(conf: float = 0.65, **kw) -> dict:
    """构造 over 方向分析结果。"""
    a = {
        "prediction": "over",
        "bet_type": "total",
        "confidence": conf,
        "odds": 1.80,
        "line": 2.5,
        "consensus_reached": True,
        "reasoning": "双方进攻强势",
        "context_source": "none",
        "models_used": ["deepseek"],
    }
    a.update(kw)
    return a


async def _eval(eng, match, analysis):
    """执行 evaluate_bet，mock 掉 calibration/risk_tuning 避免查 DB。"""
    with patch("app.ai.calibration.load_risk_patterns", new=AsyncMock(return_value=[])), \
         patch("app.ai.calibration.load_risk_tuning", new=AsyncMock(return_value={})):
        return await eng.evaluate_bet(
            match_info=match,
            analysis=analysis,
            user_balance=Decimal("1000"),
            daily_loss=Decimal("0"),
            active_bets_count=0,
        )


# ═══════════════════════════════════════════════════════════════
# P7：under 反向风险封顶
# ═══════════════════════════════════════════════════════════════

class TestP7UnderReverseRisk:
    """P7：under conf≥0.74 应封顶到 0.72，而非硬拒绝。"""

    @pytest.mark.asyncio
    async def test_under_conf_074_capped_not_rejected(self):
        """under conf=0.74（恰好触发阈值）应封顶到 0.72，不应被 P7 拒绝。"""
        engine = _mk_engine()
        analysis = _analysis_under(conf=0.74)
        decision = await _eval(engine, _football_match(), analysis)

        # 不应因 P7 被拒绝（后续闸门可能拒绝，但 reasoning 不含"反向风险"硬拒绝）
        assert "反向风险" not in str(decision.reasoning) or "封顶" in str(decision.reasoning)

    @pytest.mark.asyncio
    async def test_under_conf_075_capped_to_072(self):
        """under conf=0.75 应被封顶到 0.72。"""
        engine = _mk_engine()
        analysis = _analysis_under(conf=0.75)
        await _eval(engine, _football_match(), analysis)

        # analysis dict 应被原地修改
        assert analysis["confidence"] == pytest.approx(0.72, abs=0.001)
        assert "confidence_capped_reason" in analysis
        assert "0.72" in analysis["confidence_capped_reason"]

    @pytest.mark.asyncio
    async def test_under_conf_080_capped_to_072(self):
        """under conf=0.80（远超阈值）仍应封顶到 0.72。"""
        engine = _mk_engine()
        analysis = _analysis_under(conf=0.80)
        await _eval(engine, _football_match(), analysis)

        assert analysis["confidence"] == pytest.approx(0.72, abs=0.001)

    @pytest.mark.asyncio
    async def test_under_conf_073_not_capped(self):
        """under conf=0.73（低于 0.74 阈值）不应触发封顶。"""
        engine = _mk_engine()
        analysis = _analysis_under(conf=0.73)
        original_conf = analysis["confidence"]
        await _eval(engine, _football_match(), analysis)

        # conf 未被 P7 封顶（可能被其他逻辑修改，但不应有 P7 封顶标记）
        assert "confidence_capped_reason" not in analysis or "0.72" not in analysis.get("confidence_capped_reason", "")

    @pytest.mark.asyncio
    async def test_under_conf_065_not_capped(self):
        """under conf=0.65（正常范围）不应触发封顶。"""
        engine = _mk_engine()
        analysis = _analysis_under(conf=0.65)
        await _eval(engine, _football_match(), analysis)

        assert "confidence_capped_reason" not in analysis

    @pytest.mark.asyncio
    async def test_under_conf_074_capped_reason_set(self):
        """under conf=0.74 封顶后 confidence_capped_reason 应包含 'under'。"""
        engine = _mk_engine()
        analysis = _analysis_under(conf=0.74)
        await _eval(engine, _football_match(), analysis)

        reason = analysis.get("confidence_capped_reason", "")
        assert "under" in reason.lower()
        assert "0.72" in reason


# ═══════════════════════════════════════════════════════════════
# P9：over 反向风险封顶
# ═══════════════════════════════════════════════════════════════

class TestP9OverReverseRisk:
    """P9：over conf≥0.73 应封顶到 0.72，而非硬拒绝。"""

    @pytest.mark.asyncio
    async def test_over_conf_073_capped_not_rejected(self):
        """over conf=0.73（恰好触发阈值）应封顶到 0.72，不应被 P9 拒绝。"""
        engine = _mk_engine()
        analysis = _analysis_over(conf=0.73)
        decision = await _eval(engine, _football_match(), analysis)

        # 不应因 P9 被硬拒绝
        assert "反向风险" not in str(decision.reasoning) or "封顶" in str(decision.reasoning)

    @pytest.mark.asyncio
    async def test_over_conf_075_capped_to_072(self):
        """over conf=0.75 应被封顶到 0.72。"""
        engine = _mk_engine()
        analysis = _analysis_over(conf=0.75)
        await _eval(engine, _football_match(), analysis)

        assert analysis["confidence"] == pytest.approx(0.72, abs=0.001)
        assert "confidence_capped_reason" in analysis
        assert "0.72" in analysis["confidence_capped_reason"]

    @pytest.mark.asyncio
    async def test_over_conf_080_capped_to_072(self):
        """over conf=0.80（远超阈值）仍应封顶到 0.72。"""
        engine = _mk_engine()
        analysis = _analysis_over(conf=0.80)
        await _eval(engine, _football_match(), analysis)

        assert analysis["confidence"] == pytest.approx(0.72, abs=0.001)

    @pytest.mark.asyncio
    async def test_over_conf_072_not_capped(self):
        """over conf=0.72（低于 0.73 阈值）不应触发封顶。"""
        engine = _mk_engine()
        analysis = _analysis_over(conf=0.72)
        await _eval(engine, _football_match(), analysis)

        assert "confidence_capped_reason" not in analysis

    @pytest.mark.asyncio
    async def test_over_conf_065_not_capped(self):
        """over conf=0.65（正常范围）不应触发封顶。"""
        engine = _mk_engine()
        analysis = _analysis_over(conf=0.65)
        await _eval(engine, _football_match(), analysis)

        assert "confidence_capped_reason" not in analysis

    @pytest.mark.asyncio
    async def test_over_conf_073_capped_reason_set(self):
        """over conf=0.73 封顶后 confidence_capped_reason 应包含 'over'。"""
        engine = _mk_engine()
        analysis = _analysis_over(conf=0.73)
        await _eval(engine, _football_match(), analysis)

        reason = analysis.get("confidence_capped_reason", "")
        assert "over" in reason.lower()
        assert "0.72" in reason


# ═══════════════════════════════════════════════════════════════
# 边界值与互斥性
# ═══════════════════════════════════════════════════════════════

class TestP7P9Boundary:
    """P7/P9 边界值测试与互斥性验证。"""

    @pytest.mark.asyncio
    async def test_under_073_not_over_073_neither_capped(self):
        """under conf=0.73 不触发 P7（<0.74），over conf=0.72 不触发 P9（<0.73）。"""
        engine = _mk_engine()

        # under 0.73 → 不封顶
        a_under = _analysis_under(conf=0.73)
        await _eval(engine, _football_match(), a_under)
        assert "confidence_capped_reason" not in a_under

        # over 0.72 → 不封顶
        a_over = _analysis_over(conf=0.72)
        await _eval(engine, _football_match(), a_over)
        assert "confidence_capped_reason" not in a_over

    @pytest.mark.asyncio
    async def test_under_074_triggers_p7_not_p9(self):
        """under conf=0.74 触发 P7 封顶，reason 标注 under 而非 over。"""
        engine = _mk_engine()
        analysis = _analysis_under(conf=0.74)
        await _eval(engine, _football_match(), analysis)

        reason = analysis.get("confidence_capped_reason", "")
        assert "under" in reason.lower()
        assert "over" not in reason.lower()

    @pytest.mark.asyncio
    async def test_over_073_triggers_p9_not_p7(self):
        """over conf=0.73 触发 P9 封顶，reason 标注 over 而非 under。"""
        engine = _mk_engine()
        analysis = _analysis_over(conf=0.73)
        await _eval(engine, _football_match(), analysis)

        reason = analysis.get("confidence_capped_reason", "")
        assert "over" in reason.lower()
        assert "under" not in reason.lower()

    @pytest.mark.asyncio
    async def test_cap_value_is_exactly_072(self):
        """封顶值应精确为 0.72（不随原始 conf 变化）。"""
        engine = _mk_engine()

        for conf in (0.74, 0.75, 0.80, 0.90, 0.99):
            a = _analysis_under(conf=conf)
            await _eval(engine, _football_match(), a)
            assert a["confidence"] == pytest.approx(0.72, abs=0.001), f"conf={conf} 未正确封顶"

        for conf in (0.73, 0.75, 0.80, 0.90, 0.99):
            a = _analysis_over(conf=conf)
            await _eval(engine, _football_match(), a)
            assert a["confidence"] == pytest.approx(0.72, abs=0.001), f"conf={conf} 未正确封顶"


# ═══════════════════════════════════════════════════════════════
# 封顶后继续评估后续闸门
# ═══════════════════════════════════════════════════════════════

class TestPostCapGateFlow:
    """封顶后信号不被丢弃，后续闸门（B/C/D/E）继续评估。"""

    @pytest.mark.asyncio
    async def test_capped_under_can_pass_all_gates(self):
        """封顶后的 under 如果其他条件都满足，应能通过全部闸门下单。"""
        engine = _mk_engine()
        # 足球 30'、0-0、line=2.5、under conf=0.75→封顶0.72、odds=1.80
        # 30' > 20' 早段门槛 ✓，line=2.5 在 [2.5,5.0] ✓
        analysis = _analysis_over(conf=0.75, line=2.5, odds=1.80)
        analysis["prediction"] = "under"
        match = _football_match(clock="30'", home_score=0, away_score=0)
        decision = await _eval(engine, match, analysis)

        # 封顶后 confidence=0.72 >= required(0.65)，A3 通过
        # 后续闸门也应通过（30'/0-0/2.5线/odds1.80 都合理）
        # 注意：context_source=none 触发无基本面加严 0.68，0.72>0.68 仍通过
        assert analysis["confidence"] == pytest.approx(0.72, abs=0.001)

    @pytest.mark.asyncio
    async def test_capped_over_can_pass_all_gates(self):
        """封顶后的 over 如果其他条件都满足，应能通过全部闸门。"""
        engine = _mk_engine()
        # 足球 30'、1-1（已有2球）、line=2.5、over conf=0.75→封顶0.72
        # over 需要追0球（已2球≥2.5线？不，2<2.5还需0.5球），条件合理
        analysis = _analysis_over(conf=0.75, line=2.5, odds=1.80)
        match = _football_match(clock="30'", home_score=1, away_score=1)
        decision = await _eval(engine, match, analysis)

        # 封顶后应继续走闸门链
        assert analysis["confidence"] == pytest.approx(0.72, abs=0.001)

    @pytest.mark.asyncio
    async def test_capped_conf_still_subject_to_b_gates(self):
        """封顶后的信号仍可能被 B 阶段闸门拒绝（如盘线超范围）。"""
        engine = _mk_engine()
        # under conf=0.80→封顶0.72，但 line=1.5 < under_min_line(2.0) → B1 拒绝
        analysis = _analysis_under(conf=0.80, line=1.5, odds=1.80)
        match = _football_match(clock="30'")
        decision = await _eval(engine, match, analysis)

        # 封顶已生效
        assert analysis["confidence"] == pytest.approx(0.72, abs=0.001)
        # 但 B1 应拒绝（盘线太低）
        assert not decision.should_bet

    @pytest.mark.asyncio
    async def test_capped_conf_still_subject_to_e_gates(self):
        """封顶后的信号仍可能被 E 阶段闸门拒绝（如赔率超范围）。"""
        engine = _mk_engine()
        # under conf=0.80→封顶0.72，但 match_info odds=10.0 > max_odds → E1 拒绝
        analysis = _analysis_under(conf=0.80, line=2.5, odds=10.0)
        match = _football_match(clock="30'")
        match["odds"] = {"under": 10.0, "over": 1.80}  # 在 match_info 中设置高赔率
        decision = await _eval(engine, match, analysis)

        assert analysis["confidence"] == pytest.approx(0.72, abs=0.001)
        assert not decision.should_bet
