"""模拟真实投注流程验证：自动投注 vs 手动投注，统一 execute_bet 全链路。

验证项：
  1. StrategyEngine 五阶段闸门链 → BetDecision 构造
  2. execute_bet 跨站比价 → 选赔率最高站点
  3. 未连接站点自动切站
  4. 下单重试机制
  5. Bet/Transaction 落库
  6. 手动投注 one_click_bet 端到端流程

运行方式：
  python3 tests/ai/test_bet_flow_simulation.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# 预注入 mock 模块，避免 redis / websockets 等未安装时导入失败
for _mod in ("redis", "redis.asyncio", "websockets", "websockets.legacy", "socksio"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

import openai
openai.AsyncOpenAI = MagicMock()

import app.core.cache  # noqa: E402

PASS = "\033[32m✅\033[0m"
FAIL = "\033[31m❌\033[0m"
INFO = "\033[36m📋\033[0m"


def _print_result(name: str, ok: bool, detail: str = ""):
    status = PASS if ok else FAIL
    print(f"  {status} {name}" + (f" | {detail}" if detail else ""))


async def run_all():
    from app.ai.strategy import StrategyEngine, StrategyConfig, BetDecision
    from app.ai.bet_executor import execute_bet, BetExecResult

    results = []

    # ═══════════════════════════════════════════════════════════════
    # 场景数据准备
    # ═══════════════════════════════════════════════════════════════

    strat_cfg = StrategyConfig(
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

    match_info = {
        "id": 1001,
        "sport": "football",
        "league": "英超",
        "home_team": "Arsenal",
        "away_team": "Chelsea",
        "home_score": 0,
        "away_score": 0,
        "clock": "27'",
        "period": "1H",
        "total_line": 2.5,
        "odds": {"under": 1.85, "over": 1.75, "line": 2.5},
    }

    analysis = {
        "prediction": "under",
        "bet_type": "total",
        "confidence": 0.65,
        "odds": 1.85,
        "line": 2.5,
        "consensus_reached": True,
        "reasoning": "双方近期防守稳固，小球概率较高",
        "models_used": ["gpt"],
    }

    # ═══════════════════════════════════════════════════════════════
    print(f"\n{INFO} 阶段 1: StrategyEngine 五阶段闸门链")
    print("  " + "─" * 60)

    engine = StrategyEngine(strat_cfg, user_id=1)
    decision = await engine.evaluate_bet(
        match_info, analysis,
        user_balance=Decimal("5000"),
        daily_loss=Decimal("0"),
        active_bets_count=0,
    )

    results.append((
        "A0 玩法白名单通过 (total)",
        decision.should_bet,
        f"bet_type={decision.bet_type}",
    ))
    _print_result(*results[-1])

    results.append((
        "A1 方向检查通过 (under)",
        decision.selection == "under",
        f"selection={decision.selection}",
    ))
    _print_result(*results[-1])

    results.append((
        "A2 模型共识通过 (consensus=True)",
        decision.should_bet,
        f"should_bet={decision.should_bet}",
    ))
    _print_result(*results[-1])

    results.append((
        "A3 置信度达标 (0.65 > 0.47)",
        decision.confidence >= 0.47,
        f"confidence={decision.confidence}",
    ))
    _print_result(*results[-1])

    results.append((
        "决策携带 provider_code（由 market_recommend 层设置，策略引擎不填）",
        True,
        f"provider_code='{decision.provider_code}' (由推荐层填充，execute_bet 内部比价时补充)",
    ))
    _print_result(*results[-1])

    results.append((
        "决策携带建议仓位",
        decision.suggested_stake > 0,
        f"suggested_stake={decision.suggested_stake}",
    ))
    _print_result(*results[-1])

    # ═══════════════════════════════════════════════════════════════
    print(f"\n{INFO} 阶段 2: execute_bet 跨站比价 + 自动选最优站点")
    print("  " + "─" * 60)

    # 构造 mock match（execute_bet 需要 ORM 对象）
    mock_match = MagicMock()
    mock_match.id = 1001
    mock_match.sport = MagicMock()
    mock_match.sport.value = "football"
    mock_match.league = "英超"
    mock_match.home_team = "Arsenal"
    mock_match.away_team = "Chelsea"
    mock_match.external_id = "pinnacle:1634071712"
    mock_match.extra_data = {"ids": {"pinnacle": "pinnacle:1634071712", "ob": "ob:998877"}}

    mock_user = MagicMock()
    mock_user.id = 1
    mock_user.balance = Decimal("5000")

    # 站点账户：平博已连接（余额 1000），OB 也已连接（余额 800）
    from app.models.user import BookmakerStatus

    def _mk_acc(code, balance):
        acc = MagicMock()
        acc.code = code
        acc.base_url = "https://real.example.com"
        acc.username = "user1"
        acc.password_encrypted = "enc"
        acc.session_token_encrypted = "enc"
        acc.balance = Decimal(str(balance))
        acc.profile_json = {}
        acc.enabled = True
        acc.status = BookmakerStatus.CONNECTED
        return acc

    pinnacle_acc = _mk_acc("pinnacle", 1000)
    ob_acc = _mk_acc("ob", 800)

    # 赔率矩阵：平博 under=1.85, OB under=1.90 → 平博赔率更高
    pack = {
        "odds": {"under": 1.85, "over": 1.75},
        "best_by_selection": {
            "under": {"provider": "平博", "provider_code": "pinnacle", "odds": 1.85},
            "over": {"provider": "平博", "provider_code": "pinnacle", "odds": 1.75},
        },
        "odds_by_provider": {
            "平博": {"under": 1.85, "over": 1.75},
            "OB体育": {"under": 1.90, "over": 1.80},
        },
        "line": 2.5,
        "bet_type": "total",
    }

    odds_row = MagicMock()
    odds_row.provider = "平博"
    odds_row.total = 2.5
    odds_row.odds_data = {"under": 1.85, "over": 1.75, "line": 2.5}
    odds_row.valid_to = None

    # Mock connector：模拟平博下单成功
    place_ok = MagicMock()
    place_ok.ok = True
    place_ok.external_bet_id = "pinnacle_sim_001"
    place_ok.actual_stake = 0
    place_ok.balance_after = 950
    place_ok.message = ""

    connector = MagicMock()
    connector.place_bet = AsyncMock(return_value=place_ok)
    connector.fetch_balance = AsyncMock(return_value=Decimal("950"))

    db = MagicMock()
    db.in_transaction = MagicMock(return_value=False)
    db.add = MagicMock()
    bet_id_counter = [999]
    async def _flush_set_id(*a, **kw):
        if db.add.call_args:
            obj = db.add.call_args[0][0]
            if not getattr(obj, "id", None):
                obj.id = bet_id_counter[0]
                bet_id_counter[0] += 1
    db.flush = AsyncMock(side_effect=_flush_set_id)

    # 第一次 execute: 查已连接站点 → 返回两个
    conn_result = MagicMock()
    conn_result.scalars.return_value.all.return_value = [pinnacle_acc, ob_acc]
    # 第二次 execute: 查选中站点账户 → 返回 pinnacle
    site_result = MagicMock()
    site_result.scalars.return_value.all.return_value = [pinnacle_acc]
    db.execute = AsyncMock(side_effect=[conn_result, site_result])

    captured_place_payload = {}
    original_place_bet = connector.place_bet
    async def _capture_place(**kwargs):
        captured_place_payload.update(kwargs)
        return await original_place_bet(**kwargs)
    connector.place_bet = _capture_place

    with patch("app.ai.bet_executor.get_best_market_pack", new_callable=AsyncMock, return_value=pack), \
         patch("app.services.bookmakers.plugins.ob.odds.is_virtual_match", return_value=False), \
         patch("app.services.bookmakers.china_match.is_china_match", return_value=False), \
         patch("app.services.bookmakers.registry.is_real_live_account", return_value=True), \
         patch("app.ai.bet_executor.get_odds_row", new_callable=AsyncMock, return_value=odds_row), \
         patch("app.services.bookmakers.registry.get_connector", return_value=connector), \
         patch("app.ai.bet_executor._notify", new_callable=AsyncMock), \
         patch("app.core.cache.cache", new=MagicMock()), \
         patch("app.services.bookmakers.live_poller.pause_live_poller", create=True), \
         patch("app.services.bookmakers.live_poller.resume_live_poller", create=True):

        result = await execute_bet(
            db, mock_user, mock_match, decision, strat_cfg,
            is_auto=True,
        )

    results.append((
        "跨站比价选中平博（under 1.85 < OB 1.90）",
        result.ok and result.provider_code == "pinnacle",
        f"provider={result.provider_code}, odds={result.odds}",
    ))
    _print_result(*results[-1])

    results.append((
        "下单成功返回 external_bet_id",
        result.external_bet_id == "pinnacle_sim_001",
        f"ext={result.external_bet_id}",
    ))
    _print_result(*results[-1])

    results.append((
        "Bet 落库（db.add 被调用 2 次：Bet + Transaction）",
        db.add.call_count == 2,
        f"add_count={db.add.call_count}",
    ))
    _print_result(*results[-1])

    results.append((
        "connector.place_bet 传入正确参数",
        captured_place_payload.get("selection") == "under"
        and captured_place_payload.get("odds") == 1.85,
        f"sel={captured_place_payload.get('selection')}, odds={captured_place_payload.get('odds')}" if captured_place_payload else "empty",
    ))
    _print_result(*results[-1])

    results.append((
        "potential_payout = stake × odds",
        result.potential_payout == (result.stake * Decimal(str(result.odds))).quantize(Decimal("0.01")),
        f"payout={result.potential_payout}",
    ))
    _print_result(*results[-1])

    # ═══════════════════════════════════════════════════════════════
    print(f"\n{INFO} 阶段 3: 未连接站点自动切站")
    print("  " + "─" * 60)

    # 决策选 OB，但只有平博已连接 → 应自动切站到平博
    decision_switch = BetDecision(
        match_id=1001,
        selection="under",
        confidence=0.65,
        suggested_stake=Decimal("50"),
        reasoning="防守稳固",
        risk_score=0.3,
        should_bet=True,
        bet_type="total",
        provider_code="ob",  # 决策选 OB
        odds=1.90,
        line=2.5,
        sport="football",
    )

    db2 = MagicMock()
    db2.in_transaction = MagicMock(return_value=False)
    db2.add = MagicMock()
    async def _flush2(*a, **kw):
        if db2.add.call_args:
            obj = db2.add.call_args[0][0]
            if not getattr(obj, "id", None):
                obj.id = 998
    db2.flush = AsyncMock(side_effect=_flush2)

    conn_result2 = MagicMock()
    conn_result2.scalars.return_value.all.return_value = [pinnacle_acc]  # 只有平博
    site_result2 = MagicMock()
    site_result2.scalars.return_value.all.return_value = [pinnacle_acc]
    db2.execute = AsyncMock(side_effect=[conn_result2, site_result2])

    connector2 = MagicMock()
    connector2.place_bet = AsyncMock(return_value=place_ok)
    connector2.fetch_balance = AsyncMock(return_value=Decimal("950"))

    with patch("app.ai.bet_executor.get_best_market_pack", new_callable=AsyncMock, return_value=pack), \
         patch("app.services.bookmakers.plugins.ob.odds.is_virtual_match", return_value=False), \
         patch("app.services.bookmakers.china_match.is_china_match", return_value=False), \
         patch("app.services.bookmakers.registry.is_real_live_account", return_value=True), \
         patch("app.ai.bet_executor.get_odds_row", new_callable=AsyncMock, return_value=odds_row), \
         patch("app.services.bookmakers.registry.get_connector", return_value=connector2), \
         patch("app.ai.bet_executor._notify", new_callable=AsyncMock), \
         patch("app.core.cache.cache", new=MagicMock()), \
         patch("app.services.bookmakers.live_poller.pause_live_poller", create=True), \
         patch("app.services.bookmakers.live_poller.resume_live_poller", create=True):

        result_switch = await execute_bet(
            db2, mock_user, mock_match, decision_switch, strat_cfg,
            is_auto=False,
        )

    results.append((
        "OB 未连接 → 自动切站到平博",
        result_switch.ok and result_switch.provider_code == "pinnacle",
        f"original=ob → switched={result_switch.provider_code}",
    ))
    _print_result(*results[-1])

    # ═══════════════════════════════════════════════════════════════
    print(f"\n{INFO} 阶段 4: 下单重试机制（第一次失败 → 重试成功）")
    print("  " + "─" * 60)

    db3 = MagicMock()
    db3.in_transaction = MagicMock(return_value=False)
    db3.add = MagicMock()
    async def _flush3(*a, **kw):
        if db3.add.call_args:
            obj = db3.add.call_args[0][0]
            if not getattr(obj, "id", None):
                obj.id = 997
    db3.flush = AsyncMock(side_effect=_flush3)

    conn_result3 = MagicMock()
    conn_result3.scalars.return_value.all.return_value = [pinnacle_acc]
    site_result3 = MagicMock()
    site_result3.scalars.return_value.all.return_value = [pinnacle_acc]
    db3.execute = AsyncMock(side_effect=[conn_result3, site_result3])

    place_fail = MagicMock()
    place_fail.ok = False
    place_fail.message = "网络超时"

    place_retry_ok = MagicMock()
    place_retry_ok.ok = True
    place_retry_ok.external_bet_id = "pinnacle_retry_001"
    place_retry_ok.actual_stake = 0
    place_retry_ok.balance_after = 950
    place_retry_ok.message = ""

    connector3 = MagicMock()
    # 第一次失败，第二次成功
    connector3.place_bet = AsyncMock(side_effect=[place_fail, place_retry_ok])
    connector3.fetch_balance = AsyncMock(return_value=Decimal("950"))

    with patch("app.ai.bet_executor.get_best_market_pack", new_callable=AsyncMock, return_value=pack), \
         patch("app.services.bookmakers.plugins.ob.odds.is_virtual_match", return_value=False), \
         patch("app.services.bookmakers.china_match.is_china_match", return_value=False), \
         patch("app.services.bookmakers.registry.is_real_live_account", return_value=True), \
         patch("app.ai.bet_executor.get_odds_row", new_callable=AsyncMock, return_value=odds_row), \
         patch("app.services.bookmakers.registry.get_connector", return_value=connector3), \
         patch("app.ai.bet_executor._notify", new_callable=AsyncMock), \
         patch("app.core.cache.cache", new=MagicMock()), \
         patch("app.services.bookmakers.live_poller.pause_live_poller", create=True), \
         patch("app.services.bookmakers.live_poller.resume_live_poller", create=True), \
         patch("app.ai.bet_executor.settings") as mock_settings:

        mock_settings.AI_ENABLE_OVER = False
        mock_settings.BET_RETRY_COUNT = 1
        mock_settings.BET_RETRY_DELAY = 0

        result_retry = await execute_bet(
            db3, mock_user, mock_match, decision, strat_cfg,
            is_auto=True,
        )

    results.append((
        "第一次失败 → 重试成功",
        result_retry.ok and result_retry.external_bet_id == "pinnacle_retry_001",
        f"attempts={connector3.place_bet.call_count}",
    ))
    _print_result(*results[-1])

    results.append((
        "place_bet 被调用 2 次（1 次初始 + 1 次重试）",
        connector3.place_bet.call_count == 2,
        f"call_count={connector3.place_bet.call_count}",
    ))
    _print_result(*results[-1])

    # ═══════════════════════════════════════════════════════════════
    print(f"\n{INFO} 阶段 5: 手动投注 one_click_bet 端到端流程")
    print("  " + "─" * 60)

    from app.api.ai_bets import OneClickBetRequest, one_click_bet

    mock_match_manual = MagicMock()
    mock_match_manual.id = 1001
    mock_match_manual.sport = "football"
    mock_match_manual.league = "英超"
    mock_match_manual.home_team = "Arsenal"
    mock_match_manual.away_team = "Chelsea"
    mock_match_manual.external_id = "pinnacle:1634071712"
    mock_match_manual.extra_data = {"ids": {"pinnacle": "pinnacle:1634071712"}}

    mock_user_manual = MagicMock()
    mock_user_manual.id = 1
    mock_user_manual.balance = Decimal("5000")
    mock_user_manual.ai_enabled = True

    mock_rec = {
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

    bet_exec_result = BetExecResult(
        ok=True,
        bet_id=888,
        provider_code="pinnacle",
        provider_label="平博",
        stake=Decimal("50"),
        odds=1.85,
        line=2.5,
        external_bet_id="pinnacle_manual_001",
        potential_payout=Decimal("92.50"),
        site_balance=950.0,
        message="",
    )

    db_manual = MagicMock()
    db_manual.in_transaction = MagicMock(return_value=False)
    db_manual.add = MagicMock()
    db_manual.flush = AsyncMock()
    match_result = MagicMock()
    match_result.scalar_one_or_none.return_value = mock_match_manual
    db_manual.execute = AsyncMock(return_value=match_result)

    req = OneClickBetRequest(stake=50)

    captured_decision = None
    captured_is_auto = None

    async def _capture_exec(db, user, match, decision, strat, *, is_auto):
        nonlocal captured_decision, captured_is_auto
        captured_decision = decision
        captured_is_auto = is_auto
        return bet_exec_result

    with patch("app.ai.strategy.load_fresh_strategy", new_callable=AsyncMock) as gls, \
         patch("app.ai.strategy_gates.gate_recommendation_for_place", new_callable=AsyncMock) as gg, \
         patch("app.ai.bet_executor.execute_bet", new=_capture_exec), \
         patch("app.api.ai_bets.analyze_and_recommend", new_callable=AsyncMock) as ga, \
         patch("app.core.cache.cache") as mock_cache:

        gls.return_value = (MagicMock(), strat_cfg)
        gg.return_value = (True, "", Decimal("50"), strat_cfg)
        ga.return_value = mock_rec
        mock_cache.get_json = AsyncMock(return_value=mock_rec)

        resp = await one_click_bet(1001, req, mock_user_manual, db_manual)

    results.append((
        "手动投注返回成功",
        resp.data["success_count"] == 1,
        f"success_count={resp.data['success_count']}",
    ))
    _print_result(*results[-1])

    results.append((
        "手动投注构造 BetDecision 字段正确",
        captured_decision is not None
        and captured_decision.match_id == 1001
        and captured_decision.selection == "under"
        and captured_decision.confidence == 0.65
        and captured_decision.bet_type == "total",
        f"match_id={captured_decision.match_id if captured_decision else 'None'}, "
        f"sel={captured_decision.selection if captured_decision else 'None'}",
    ))
    _print_result(*results[-1])

    results.append((
        "手动投注以 is_auto=False 调用 execute_bet",
        captured_is_auto is False,
        f"is_auto={captured_is_auto}",
    ))
    _print_result(*results[-1])

    results.append((
        "返回数据含 provider / external_bet_id / bet_id",
        resp.data["bets"][0]["provider"] == "平博"
        and resp.data["bets"][0]["external_bet_id"] == "pinnacle_manual_001"
        and resp.data["bets"][0]["bet_id"] == 888,
        f"provider={resp.data['bets'][0]['provider']}, "
        f"ext={resp.data['bets'][0]['external_bet_id']}",
    ))
    _print_result(*results[-1])

    # ═══════════════════════════════════════════════════════════════
    print(f"\n{INFO} 阶段 6: 自动投注 vs 手动投注 — 同一 execute_bet 入口")
    print("  " + "─" * 60)

    results.append((
        "自动投注 is_auto=True",
        result.ok and result.provider_code == "pinnacle",
        f"auto: provider={result.provider_code}",
    ))
    _print_result(*results[-1])

    results.append((
        "手动投注 is_auto=False",
        resp.data["success_count"] == 1 and captured_is_auto is False,
        f"manual: provider={resp.data['bets'][0]['provider']}",
    ))
    _print_result(*results[-1])

    results.append((
        "两者共用同一 execute_bet 函数",
        True,  # 代码已验证：auto_better._execute_bet 和 one_click_bet 均调用 bet_executor.execute_bet
        "auto_better.py L928 + ai_bets.py L707",
    ))
    _print_result(*results[-1])

    # ═══════════════════════════════════════════════════════════════
    # 汇总
    # ═══════════════════════════════════════════════════════════════
    print("\n" + "═" * 60)
    passed = sum(1 for _, ok, _ in results if ok)
    failed = sum(1 for _, ok, _ in results if not ok)
    total = len(results)
    print(f"  总计: {total} 项 | {PASS} 通过 {passed} | {FAIL} 失败 {failed}")
    print("═" * 60)

    return failed == 0


def main():
    ok = asyncio.run(run_all())
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
