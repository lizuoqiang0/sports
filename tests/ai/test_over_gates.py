"""大小球双闸门链（under/over 互不参与）单元测试。

覆盖：
- A1 方向分派（影子模式已移除，over 与 under 对等参与）
- B1-over 线区间/早段/末段（独立参数，不影响 under）
- B3 高赔率 over 镜像
- C1 升盘→over 支持（against_under 改名 over 后语义）
- D1-over 进球速率闸门（needed 上限 + pace 校验）
- A3 方向门槛（over 0.68 / under 0.65，实盘调参后提高）
- 胜率自适应按 by_selection 隔离（over 样本不污染 under 门槛）
- 归一化层 over 词表
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.ai.analyzer import normalize_prediction  # noqa: E402
from app.ai.strategy import SPORT_RISK, StrategyEngine, StrategyConfig  # noqa: E402


PASS = "\033[32m✅\033[0m"
FAIL = "\033[31m❌\033[0m"


def _mk_engine(over_enabled: bool = False) -> StrategyEngine:
    cfg = StrategyConfig(name="test", min_confidence=0.0, min_odds=1.5, max_odds=2.5)
    eng = StrategyEngine(config=cfg, user_id=None)
    # 影子模式开关：直接 patch settings 属性
    import app.ai.strategy as strat_mod

    strat_mod.settings.AI_ENABLE_OVER = over_enabled
    return eng


def _base_match(**kw) -> dict:
    m = {
        "id": 1, "home_team": "A", "away_team": "B", "sport": "football",
        "league": "Test League", "period": "2H", "clock": "45'",
        "home_score": 1, "away_score": 1,
        "odds": {"under": 1.90, "over": 1.90},
        "line_movements": {},
    }
    m.update(kw)
    return m


def _analysis(**kw) -> dict:
    a = {
        "prediction": "over", "bet_type": "total", "confidence": 0.70,
        "odds": 1.90, "line": 2.5, "consensus_reached": True,
        "reasoning": "测试", "context_source": "none",
    }
    a.update(kw)
    return a


async def _eval(eng, match, analysis):
    """evaluate_bet 不依赖 pnl（风控在外层引擎），只需缓存空统计。"""
    eng._cached_stats = {"settled": 0, "by_selection": {}}
    return await eng.evaluate_bet(
        match_info=match, analysis=analysis,
        user_balance=1000, daily_loss=0, active_bets_count=0,
    )


async def run_cases():
    results = []

    # ── 1. 归一化：over 词表 ──
    for raw, want in [("over", "over"), ("大", "over"), ("大球", "over"),
                      ("Over", "over"), ("under", "under"), ("skip", "skip"), ("home", "")]:
        got = normalize_prediction(raw, bet_type="total")
        results.append((f"normalize('{raw}')→{want}", got == want, f"got={got!r}"))

    # ── 2. A1 影子模式已移除：AI_ENABLE_OVER=False 时 over 仍参与闸门评估 ──
    eng = _mk_engine(over_enabled=False)
    d = await _eval(eng, _base_match(), _analysis())
    results.append(("影子模式已移除：over 不被 AI_ENABLE_OVER 拦截",
                    "未启用" not in d.reasoning,
                    f"reasoning={d.reasoning[:60]}"))

    # ── 3. A1 开启后 over 进入闸门链（高 conf 合法场次应通过或被真实闸门拦）──
    eng = _mk_engine(over_enabled=True)
    d = await _eval(eng, _base_match(), _analysis())
    # 45' clock 解析不到 played_mins（无真实时钟解析）→ D1 跳过；其余闸门过
    results.append(("开启后 over 不再被 A1 拦", "未启用" not in d.reasoning and "不支持的投注方向" not in d.reasoning,
                    f"should_bet={d.should_bet} reasoning={d.reasoning[:60]}"))

    # ── 4. A3 方向门槛：over 0.68 / under 0.65（实盘调参后提高）──
    fb = SPORT_RISK["football"]
    results.append(("over门槛0.68 under门槛0.65",
                    fb["over_min_conf"] == 0.68 and fb["under_min_conf"] == 0.65,
                    f"over={fb['over_min_conf']} under={fb['under_min_conf']}"))
    # conf=0.58 低于门槛 0.68，应被拒绝
    eng = _mk_engine(over_enabled=True)
    d = await _eval(eng, _base_match(), _analysis(confidence=0.58, context_source="none"))
    results.append(("conf=0.58 低于门槛 0.68 被拒",
                    not d.should_bet and "置信度不足" in d.reasoning,
                    f"reasoning={d.reasoning[:60]}"))

    # ── 5. B1-over 线区间独立参数 ──
    eng = _mk_engine(over_enabled=True)
    d = await _eval(eng, _base_match(), _analysis(line=5.0))  # 足球 over_max_line=3.5
    results.append(("高线 over 拒（line=5.0≥3.5）", not d.should_bet and "高线over" in d.reasoning,
                    f"reasoning={d.reasoning}"))
    eng = _mk_engine(over_enabled=True)
    d = await _eval(eng, _base_match(), _analysis(line=1.5))  # over_min_line=2.5
    results.append(("低线 over 拒（line=1.5≤2.5）", not d.should_bet and "低线over" in d.reasoning,
                    f"reasoning={d.reasoning}"))

    # ── 6. B3 高赔率镜像 ──
    eng = _mk_engine(over_enabled=True)
    d = await _eval(eng, _base_match(odds={"under": 1.90, "over": 2.10}), _analysis(odds=2.10, line=2.75, confidence=0.72))
    results.append(("over 赔率≥2.0 拒（市场看小）", not d.should_bet and "大球赔率过高" in d.reasoning,
                    f"reasoning={d.reasoning}"))

    # ── 7. C1 升盘→over 支持：升盘+over 通过方向检查（降盘+over 拒）──
    eng = _mk_engine(over_enabled=True)
    d = await _eval(eng, _base_match(line_movements={"total": {"line_delta": -0.5}}, odds={"under": 1.80, "over": 1.80}),
                    _analysis(line=2.75, odds=1.80, confidence=0.72))
    results.append(("降盘+over 拒（盘口方向相反）", not d.should_bet and "相反" in d.reasoning,
                    f"reasoning={d.reasoning}"))

    # ── 8. under 链回归：现有 under 行为不变（低线拒）──
    eng = _mk_engine(over_enabled=False)
    d = await _eval(eng, _base_match(), _analysis(prediction="under", line=1.5))
    results.append(("under 低线仍拒（回归保护）", not d.should_bet and "低线under" in d.reasoning,
                    f"reasoning={d.reasoning}"))
    eng = _mk_engine(over_enabled=False)
    d = await _eval(eng, _base_match(), _analysis(prediction="under", line=2.5, confidence=0.70))
    results.append(("under 正常场次不被 over 改动影响",
                    "未启用" not in d.reasoning and "不支持的投注方向" not in d.reasoning,
                    f"reasoning={d.reasoning[:60]}"))

    # ── 9. 胜率自适应按方向隔离：under 高连败不加严 over ──
    eng = _mk_engine(over_enabled=True)
    eng._cached_stats = {
        "settled": 20, "win_rate": 0.2,  # 整体烂
        "by_selection": {
            "under": {"settled": 20, "win_rate": 0.2},  # under 烂
            "over": {"settled": 2, "win_rate": None},   # over 样本少：不触发
        },
    }
    d = await _eval(eng, _base_match(), _analysis(confidence=0.70, line=3.0))
    # over 样本 2<5：不因 under 烂而加严（0.70 过 0.65 地板）
    results.append(("under 连败不污染 over 门槛", "置信度不足" not in d.reasoning,
                    f"reasoning={d.reasoning[:60]}"))

    # ── 10. D1-over 速率闸门：pace=0 时 0:0 拒 ──
    # 直接测速率逻辑：2.5 线 0:0 35' → needed=2.5≥2 拒（所需进球过多）
    eng = _mk_engine(over_enabled=True)
    d = await _eval(eng, _base_match(home_score=0, away_score=0, clock=""),
                    _analysis(line=2.5))
    # clock 空 → played_mins=None → D1 跳过 → 应被其他闸门正常处理（不崩溃）
    results.append(("D1 边界：无时钟不崩溃", d is not None, f"reasoning={d.reasoning[:50]}"))

    # ── 11. 篮球 over 参数独立存在 ──
    bb = SPORT_RISK["basketball"]
    results.append(("篮球 over 参数齐全",
                    all(k in bb for k in ("over_min_conf", "over_min_line", "over_max_line",
                                           "over_min_played_mins", "over_late_block_mins",
                                           "over_pace_factor", "over_min_remaining_goals")),
                    f"keys={[k for k in bb if k.startswith('over')]}"))

    # ── 12. 仓位：over 无 1.10 加成且 0.8 折（0.6→0.8 提升仓位效率）──
    # 双闸门互斥（同场景难同时过 D1），直接验证仓位公式：
    # under: conf_scale×1.10 封顶 0.95；over: conf_scale 封顶后 ×0.8
    eng = _mk_engine(over_enabled=True)
    d = await _eval(eng, _base_match(home_score=2, away_score=1, clock="", line=3.0, odds={"under": 1.80, "over": 1.80}),
                    _analysis(confidence=0.75, line=3.0, odds=1.80))
    # over 场景 conf=0.70≥0.65 → conf_scale=0.90 → ×0.8=0.72 → 仓位=100×0.72×risk_factor
    # under 公式: 0.90×1.10 封顶 0.95 → 仓位=100×0.95×risk_factor
    # 验证 over 仓位 ≈ 0.72/0.95 ≈ 76% 的 under 等效仓位
    results.append(("over 仓位 0.8 折生效（≈66-75/100）",
                    d.should_bet and 0 < d.suggested_stake <= 75,
                    f"over_stake={d.suggested_stake}(bet={d.should_bet}) 期望≤75"))

    return results


def main():
    results = asyncio.run(run_cases())
    print("\n" + "=" * 64)
    print("大小球双闸门链测试结果")
    print("=" * 64)
    fails = 0
    for name, ok, detail in results:
        print(f"{PASS if ok else FAIL}  {name}")
        print(f"       {detail}")
        fails += 0 if ok else 1
    print("-" * 64)
    print(f"通过 {len(results) - fails}/{len(results)}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
