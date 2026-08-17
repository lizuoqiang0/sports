"""下单流程测试：验证全链路闸门 + 仓位计算"""
import asyncio
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.ai.strategy import StrategyEngine, StrategyConfig, SPORT_RISK

async def test_gate_chain():
    engine = StrategyEngine(
        config=StrategyConfig(
            name="test",
            max_bet_amount=20.0,
            max_daily_bets=10,
            stop_loss=100.0,
            take_profit=200.0,
            use_llm_analysis=True,
            min_confidence=0.0,
            min_odds=1.01,
            max_odds=99.0,
        ),
        user_id=1,
    )

    results = []

    # ── 测试1: under 正常通过 ──
    print("\n测试1: under 小球 football line=2.5 conf=0.65 → 应通过")
    d = await engine.evaluate_bet(
        match_info={"id": 2, "home_team": "A", "away_team": "B", "sport": "football",
                    "odds": {"under": 1.95}, "provider_code": "pinnacle",
                    "league": "英超", "period": "", "clock": "", "home_score": 0, "away_score": 0},
        analysis={"prediction": "under", "confidence": 0.65, "odds": 1.95,
                  "consensus_reached": True, "reasoning": "test", "line": 2.5,
                  "context_source": "api", "provider_code": "pinnacle"},
        user_balance=Decimal("100"), daily_loss=Decimal("0"), active_bets_count=0,
    )
    status = "PASS" if d.should_bet else "FAIL"
    print(f"  [{status}] should_bet={d.should_bet} stake={d.suggested_stake} sel={d.selection}")
    results.append(("under正常通过", status == "PASS"))

    # ── 测试3: under 低线拒绝 ──
    print("\n测试3: under football line=1.0 → B1 低线应拒绝")
    d = await engine.evaluate_bet(
        match_info={"id": 3, "home_team": "A", "away_team": "B", "sport": "football",
                    "odds": {"under": 1.65}, "provider_code": "pinnacle",
                    "league": "英超", "period": "", "clock": "", "home_score": 0, "away_score": 0},
        analysis={"prediction": "under", "confidence": 0.65, "odds": 1.65,
                  "consensus_reached": True, "reasoning": "test", "line": 1.0,
                  "context_source": "api", "provider_code": "pinnacle"},
        user_balance=Decimal("100"), daily_loss=Decimal("0"), active_bets_count=0,
    )
    status = "PASS" if not d.should_bet and "低线" in d.reasoning else "FAIL"
    print(f"  [{status}] should_bet={d.should_bet} reason={d.reasoning[:80]}")
    results.append(("under低线拒绝", status == "PASS"))

    # ── 测试4: under 高赔率拒绝 ──
    print("\n测试4: under odds=2.10 conf=0.62 有基本面 → B3 高赔率应拒绝")
    d = await engine.evaluate_bet(
        match_info={"id": 4, "home_team": "A", "away_team": "B", "sport": "football",
                    "odds": {"under": 2.10}, "provider_code": "pinnacle",
                    "league": "英超", "period": "", "clock": "", "home_score": 0, "away_score": 0},
        analysis={"prediction": "under", "confidence": 0.62, "odds": 2.10,
                  "consensus_reached": True, "reasoning": "test", "line": 2.5,
                  "context_source": "api", "provider_code": "pinnacle"},
        user_balance=Decimal("100"), daily_loss=Decimal("0"), active_bets_count=0,
    )
    status = "PASS" if not d.should_bet and "赔率过高" in d.reasoning else "FAIL"
    print(f"  [{status}] should_bet={d.should_bet} reason={d.reasoning[:80]}")
    results.append(("under高赔率拒绝", status == "PASS"))

    # ── 测试5: under 置信度不足 ──
    print("\n测试5: under conf=0.40 无基本面 → A3 应拒绝 (要求≥0.58)")
    d = await engine.evaluate_bet(
        match_info={"id": 5, "home_team": "A", "away_team": "B", "sport": "football",
                    "odds": {"under": 1.95}, "provider_code": "pinnacle",
                    "league": "英超", "period": "", "clock": "", "home_score": 0, "away_score": 0},
        analysis={"prediction": "under", "confidence": 0.40, "odds": 1.95,
                  "consensus_reached": True, "reasoning": "test", "line": 2.5,
                  "context_source": "", "provider_code": "pinnacle"},
        user_balance=Decimal("100"), daily_loss=Decimal("0"), active_bets_count=0,
    )
    status = "PASS" if not d.should_bet and "置信度不足" in d.reasoning else "FAIL"
    print(f"  [{status}] should_bet={d.should_bet} reason={d.reasoning[:80]}")
    results.append(("under置信度不足", status == "PASS"))

    # ── 测试6: skip 方向 → A1 拒绝 ──
    print("\n测试6: prediction=skip → A1 应拒绝")
    d = await engine.evaluate_bet(
        match_info={"id": 6, "home_team": "A", "away_team": "B", "sport": "football",
                    "odds": {"under": 1.95}, "provider_code": "pinnacle",
                    "league": "英超", "period": "", "clock": "", "home_score": 0, "away_score": 0},
        analysis={"prediction": "skip", "confidence": 0.65, "odds": 1.95,
                  "consensus_reached": True, "reasoning": "test", "line": 2.5,
                  "context_source": "api", "provider_code": "pinnacle"},
        user_balance=Decimal("100"), daily_loss=Decimal("0"), active_bets_count=0,
    )
    status = "PASS" if not d.should_bet and "不支持的投注方向" in d.reasoning else "FAIL"
    print(f"  [{status}] should_bet={d.should_bet} reason={d.reasoning[:80]}")
    results.append(("skip方向拒绝", status == "PASS"))

    # ── 测试7: 联赛黑名单 ──
    print("\n测试7: league=U19 → B2 黑名单应拒绝")
    d = await engine.evaluate_bet(
        match_info={"id": 7, "home_team": "A", "away_team": "B", "sport": "football",
                    "odds": {"under": 1.95}, "provider_code": "pinnacle",
                    "league": "U19联赛", "period": "", "clock": "", "home_score": 0, "away_score": 0},
        analysis={"prediction": "under", "confidence": 0.65, "odds": 1.95,
                  "consensus_reached": True, "reasoning": "test", "line": 2.5,
                  "context_source": "api", "provider_code": "pinnacle"},
        user_balance=Decimal("100"), daily_loss=Decimal("0"), active_bets_count=0,
    )
    status = "PASS" if not d.should_bet and "青少年" in d.reasoning else "FAIL"
    print(f"  [{status}] should_bet={d.should_bet} reason={d.reasoning[:80]}")
    results.append(("联赛黑名单拒绝", status == "PASS"))

    # ── 测试8: under 仓位计算 ──
    print("\n测试8: under conf=0.62 → 仓位计算 (含×1.10加成, conf_lo=0.55)")
    d = await engine.evaluate_bet(
        match_info={"id": 8, "home_team": "A", "away_team": "B", "sport": "football",
                    "odds": {"under": 1.90}, "provider_code": "pinnacle",
                    "league": "英超", "period": "", "clock": "", "home_score": 0, "away_score": 0},
        analysis={"prediction": "under", "confidence": 0.62, "odds": 1.90,
                  "consensus_reached": True, "reasoning": "test", "line": 2.5,
                  "context_source": "api", "provider_code": "pinnacle"},
        user_balance=Decimal("100"), daily_loss=Decimal("0"), active_bets_count=0,
    )
    status = "PASS" if d.should_bet and d.suggested_stake > Decimal("8") else "FAIL"
    print(f"  [{status}] should_bet={d.should_bet} stake={d.suggested_stake} (max=20, conf_scale=0.78×1.10=0.858, prov~0.6→~10)")
    results.append(("under仓位加成", status == "PASS"))

    # ── 测试9: EV平衡 — conf < 1/odds (考虑自适应+0.05) ──
    print("\n测试9: under conf=0.61 odds=1.60 → E2 EV平衡应拒绝 (0.61 < 1/1.60=0.625)")
    d = await engine.evaluate_bet(
        match_info={"id": 9, "home_team": "A", "away_team": "B", "sport": "football",
                    "odds": {"under": 1.60}, "provider_code": "pinnacle",
                    "league": "英超", "period": "", "clock": "", "home_score": 0, "away_score": 0},
        analysis={"prediction": "under", "confidence": 0.61, "odds": 1.60,
                  "consensus_reached": True, "reasoning": "test", "line": 2.5,
                  "context_source": "api", "provider_code": "pinnacle"},
        user_balance=Decimal("100"), daily_loss=Decimal("0"), active_bets_count=0,
    )
    status = "PASS" if not d.should_bet and "负EV" in d.reasoning else "FAIL"
    print(f"  [{status}] should_bet={d.should_bet} reason={d.reasoning[:80]}")
    results.append(("EV平衡拒绝", status == "PASS"))

    # ── 测试10: basketball under 正常流程 ──
    # 篮球小分需满足三重闸门：中盘样本、降盘支持、结构化复核。
    print("\n测试10: basketball under Q2 08:00 line=180.5 conf=0.68 → 应通过")
    d = await engine.evaluate_bet(
        match_info={"id": 10, "home_team": "C", "away_team": "D", "sport": "basketball",
                    "odds": {"under": 1.95}, "provider_code": "pinnacle",
                    "league": "NBA", "period": "Q2", "clock": "08:00", "home_score": 10, "away_score": 10,
                    "line_movements": {"total": {"line_delta": -0.25}}},
        analysis={"prediction": "under", "confidence": 0.68, "odds": 1.95,
                  "consensus_reached": True, "reasoning": "test", "line": 180.5,
                  "context_source": "api", "provider_code": "pinnacle",
                  "signal_review": {
                      "triad_ready": True,
                      "verdict": "supportive",
                      "market_points": 4,
                      "fundamental_points": 4,
                      "conflict_points": 0,
                  }},
        user_balance=Decimal("100"), daily_loss=Decimal("0"), active_bets_count=0,
    )
    status = "PASS" if d.should_bet else "FAIL"
    print(f"  [{status}] should_bet={d.should_bet} stake={d.suggested_stake} sel={d.selection}")
    results.append(("basketball正常通过", status == "PASS"))

    # ── 总结 ──
    print("\n" + "=" * 60)
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"结果: {passed}/{total} 通过")
    for name, ok in results:
        print(f"  {'✅' if ok else '❌'} {name}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(test_gate_chain())
