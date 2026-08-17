"""足球/篮球模拟全链路：AI 解析 -> 策略闸门 -> 放行 -> 平博 UI 下单。

所有数据、LLM 响应和下单页面均为本地 mock，不读取真实账户或赛事。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# 此脚本注入固定 AI 响应；显式覆盖 .env，确保绝不初始化真实模型客户端。
os.environ["GPT_API_KEY"] = ""

from app.ai.analyzer import MatchAnalyzer
from app.ai.strategy import StrategyConfig, StrategyEngine, decision_passes_strategy
from app.services.bookmakers.plugins.pinnacle.bet_ui import ui_place_pinnacle_total
from scripts.mock_pinnacle_page import serve


def _historical_data() -> dict:
    low_total_rows = [
        {"home_goals": 80, "away_goals": 78},
        {"home_goals": 82, "away_goals": 76},
    ]
    return {
        "source": "api",
        "quality": {
            "source": "api",
            "completeness": 0.9,
            "fields_present": ["home_form", "away_form", "h2h", "standings", "trend"],
        },
        "home_form": {"matches": low_total_rows},
        "away_form": {"matches": low_total_rows},
        "h2h": {"summary": {"played": 4, "avg_total_goals": 158}},
        "standings": {
            "home": {"played": 10, "goals_for": 780, "goals_against": 760},
            "away": {"played": 10, "goals_for": 770, "goals_against": 750},
        },
        "trend": {"initial_odds": [{"line": 181.0}, {"line": 180.5}]},
    }


def _market(line: float, under: float) -> dict:
    return {
        "under": under,
        "line": line,
        "line_movements": {
            "total": {"line_delta": -0.25, "direction": "line_down", "change_count": 2}
        },
        "markets": {
            "total": {
                "line": line,
                "odds": {"under": under},
                "opening": {"line": line + 0.25, "odds": {"under": 1.9}},
            }
        },
    }


async def _analyze(case: dict) -> dict:
    analyzer = MatchAnalyzer()

    async def fake_call(_: str) -> dict:
        return {
            "content": json.dumps(
                {
                    "prediction": "under",
                    "bet_type": "total",
                    "line": case["line"],
                    "confidence": 0.72,
                    "reasoning": "本地模拟 AI：节奏与基本面支持小分。",
                    "key_factors": ["模拟基本面", "模拟降盘"],
                    "risk_level": "medium",
                }
            ),
            "_meta": {"latency_ms": 1},
        }

    analyzer._call_gpt = fake_call
    analyzer._record_prediction = lambda *_args: None
    analysis = await analyzer.analyze_match(
        case["match"],
        historical_data=_historical_data(),
        market_odds=case["market"],
    )
    assert analysis["prediction"] == "under", analysis
    assert analysis["bet_type"] == "total", analysis
    return analysis


async def _run_case(page, case: dict) -> tuple[bool, str]:
    analysis = await _analyze(case)
    engine = StrategyEngine(
        StrategyConfig(
            name="e2e",
            max_bet_amount=25.0,
            max_daily_bets=10,
            stop_loss=100.0,
            take_profit=200.0,
            min_confidence=0.0,
            min_odds=1.01,
            max_odds=3.0,
        ),
        user_id=0,
    )
    engine._cached_stats = {"settled": 0, "win_rate": None}

    decision = await engine.evaluate_bet(
        match_info=case["match"],
        analysis=analysis,
        user_balance=Decimal("100"),
        daily_loss=Decimal("0"),
        active_bets_count=0,
    )
    assert decision.should_bet, decision.reasoning
    allowed, reason = decision_passes_strategy(decision, engine.config)
    assert allowed, reason

    ok, detail = await ui_place_pinnacle_total(
        page,
        home=case["match"]["home_team"],
        away=case["match"]["away_team"],
        selection=decision.selection,
        odds=decision.odds,
        stake=decision.suggested_stake,
        line=decision.line,
        sport=case["match"]["sport"],
    )
    bets = await page.evaluate("() => window.__state.bets || []")
    assert ok and len(bets) == 1, detail
    bet = bets[0]
    assert bet["dir"] == "under" and str(bet["m"]["line"]) == str(case["line"]), bet
    return True, f"conf={analysis['confidence']} stake={decision.suggested_stake} ui={detail}"


async def main() -> int:
    from playwright.async_api import async_playwright

    cases = [
        {
            "name": "football",
            "line": 2.5,
            "market": _market(2.5, 1.79),
            "match": {
                "id": 9101,
                "home_team": "坎昆",
                "away_team": "德杜兰戈阿拉克兰内斯",
                "sport": "football",
                "league": "测试联赛",
                "period": "1H",
                "clock": "35'",
                "home_score": 0,
                "away_score": 0,
                "total_line": 2.5,
                "odds": {"under": 1.79},
                "line_movements": {"total": {"line_delta": -0.25}},
            },
        },
        {
            "name": "basketball",
            "line": 180.5,
            "market": _market(180.5, 1.88),
            "match": {
                "id": 9102,
                "home_team": "洛杉矶湖人",
                "away_team": "芝加哥公牛",
                "sport": "basketball",
                "league": "NBA",
                "period": "Q2",
                "clock": "08:00",
                "home_score": 10,
                "away_score": 10,
                "total_line": 180.5,
                "odds": {"under": 1.88},
                "line_movements": {"total": {"line_delta": -0.25}},
            },
        },
    ]
    server = serve(9881)
    failures: list[str] = []
    try:
        async with async_playwright() as pw:
            for case in cases:
                print(f"[RUN] {case['name']}", flush=True)
                browser = await pw.chromium.launch(headless=True)
                page = await browser.new_page()
                try:
                    await page.goto("http://127.0.0.1:9881/live", wait_until="domcontentloaded")
                    await page.wait_for_timeout(300)
                    _, detail = await _run_case(page, case)
                    print(f"[PASS] {case['name']}: {detail}")
                except Exception as exc:
                    failures.append(f"{case['name']}: {exc}")
                    print(f"[FAIL] {failures[-1]}")
                finally:
                    await browser.close()
    finally:
        server.shutdown()
    if failures:
        print("失败: " + " | ".join(failures))
        return 1
    print("全部通过：足球、篮球均完成 AI 小球解析与 mock UI 下单。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
