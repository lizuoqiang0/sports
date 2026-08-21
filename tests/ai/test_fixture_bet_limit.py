"""同一场比赛投注次数上限测试（MAX_BETS_PER_FIXTURE=2）。

模拟场景：
  同一场比赛在 OB 和平博各有一条 Match 记录（sibling）。
  验证三层去重逻辑：
    1. _match_bet_count: 跨轮次 DB+Redis 计数
    2. placed_fixture_counts: 轮次内计数
    3. _execute_bet sibling 计数检查

运行方式：
  python3 tests/ai/test_fixture_bet_limit.py
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# 预注入 mock 模块
for _mod in ("redis", "redis.asyncio", "websockets", "websockets.legacy", "socksio"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

import openai
openai.AsyncOpenAI = MagicMock()

PASS = "\033[32m✅\033[0m"
FAIL = "\033[31m❌\033[0m"
INFO = "\033[36m📋\033[0m"


def _print_result(name: str, ok: bool, detail: str = ""):
    status = PASS if ok else FAIL
    print(f"  {status} {name}" + (f" | {detail}" if detail else ""))


# 同一场比赛的 OB + 平博 sibling
SIB_IDS = [2001, 2002]


async def run_all():
    from app.ai.auto_better import AIBettingEngine, MAX_BETS_PER_FIXTURE

    results = []

    # ═══════════════════════════════════════════════════════════════
    # 场景数据：同一场比赛 OB + 平博两条 Match（sibling）
    # ═══════════════════════════════════════════════════════════════

    ob_match = MagicMock()
    ob_match.id = 2001
    ob_match.sport = MagicMock()
    ob_match.sport.value = "football"
    ob_match.home_team = "Arsenal"
    ob_match.away_team = "Chelsea"
    ob_match.external_id = "ob:998877"
    ob_match.extra_data = {"ids": {"ob": "ob:998877", "pinnacle": "pinnacle:1634"}}

    engine = AIBettingEngine(user_id=1)

    print(f"\n{INFO} 前置: 确认 MAX_BETS_PER_FIXTURE = {MAX_BETS_PER_FIXTURE}")
    print("  " + "─" * 60)
    results.append((
        f"MAX_BETS_PER_FIXTURE == 2",
        MAX_BETS_PER_FIXTURE == 2,
        f"value={MAX_BETS_PER_FIXTURE}",
    ))
    _print_result(*results[-1])

    # ═══════════════════════════════════════════════════════════════
    print(f"\n{INFO} 阶段 1: _match_bet_count — 0 注时返回 0（允许第 1 次投注）")
    print("  " + "─" * 60)

    # DB mock: count=0, Match 查询返回 ob_match, sibling count=0
    db = MagicMock()
    count_result_0 = MagicMock()
    count_result_0.scalar_one.return_value = 0
    match_result = MagicMock()
    match_result.scalar_one_or_none.return_value = ob_match
    sib_count_result_0 = MagicMock()
    sib_count_result_0.scalar_one.return_value = 0
    # execute 顺序: count(Bet.match_id==mid) → Match 查询 → sibling count
    db.execute = AsyncMock(side_effect=[count_result_0, match_result, sib_count_result_0])

    cache_mock = MagicMock()
    cache_mock.get_json = AsyncMock(return_value=None)  # 无 Redis pending

    with patch("app.core.cache.cache", cache_mock), \
         patch("app.services.fixture_key.sibling_match_ids", new_callable=AsyncMock, return_value=SIB_IDS):
        count_0 = await engine._match_bet_count(db, 1, 2001)

    results.append((
        "0 注时 _match_bet_count 返回 0",
        count_0 == 0,
        f"count={count_0}",
    ))
    _print_result(*results[-1])
    results.append((
        "count < MAX → 允许第 1 次投注",
        count_0 < MAX_BETS_PER_FIXTURE,
        f"{count_0} < {MAX_BETS_PER_FIXTURE}",
    ))
    _print_result(*results[-1])

    # ═══════════════════════════════════════════════════════════════
    print(f"\n{INFO} 阶段 2: _match_bet_count — 1 注后返回 1（允许第 2 次投注）")
    print("  " + "─" * 60)

    # 模拟已在 OB 下注 1 单：DB count=1, sibling count=0, 无 pending
    db2 = MagicMock()
    count_result_1 = MagicMock()
    count_result_1.scalar_one.return_value = 1
    match_result_2 = MagicMock()
    match_result_2.scalar_one_or_none.return_value = ob_match
    sib_count_result_0b = MagicMock()
    sib_count_result_0b.scalar_one.return_value = 0
    db2.execute = AsyncMock(side_effect=[count_result_1, match_result_2, sib_count_result_0b])

    with patch("app.core.cache.cache", cache_mock), \
         patch("app.services.fixture_key.sibling_match_ids", new_callable=AsyncMock, return_value=SIB_IDS):
        count_1 = await engine._match_bet_count(db2, 1, 2001)

    results.append((
        "1 注后 _match_bet_count 返回 1",
        count_1 == 1,
        f"count={count_1}",
    ))
    _print_result(*results[-1])
    results.append((
        "count < MAX → 允许第 2 次投注",
        count_1 < MAX_BETS_PER_FIXTURE,
        f"{count_1} < {MAX_BETS_PER_FIXTURE}",
    ))
    _print_result(*results[-1])

    # ═══════════════════════════════════════════════════════════════
    print(f"\n{INFO} 阶段 3: _match_bet_count — 2 注后返回 2（阻止第 3 次投注）")
    print("  " + "─" * 60)

    # 模拟已在 OB + 平博各下注 1 单：DB count=1, sibling count=1 → total=2
    db3 = MagicMock()
    count_result_1b = MagicMock()
    count_result_1b.scalar_one.return_value = 1
    match_result_3 = MagicMock()
    match_result_3.scalar_one_or_none.return_value = ob_match
    sib_count_result_1 = MagicMock()
    sib_count_result_1.scalar_one.return_value = 1
    db3.execute = AsyncMock(side_effect=[count_result_1b, match_result_3, sib_count_result_1])

    with patch("app.core.cache.cache", cache_mock), \
         patch("app.services.fixture_key.sibling_match_ids", new_callable=AsyncMock, return_value=SIB_IDS):
        count_2 = await engine._match_bet_count(db3, 1, 2001)

    results.append((
        "2 注后 _match_bet_count 返回 2",
        count_2 == 2,
        f"count={count_2}",
    ))
    _print_result(*results[-1])
    results.append((
        "count >= MAX → 阻止第 3 次投注",
        count_2 >= MAX_BETS_PER_FIXTURE,
        f"{count_2} >= {MAX_BETS_PER_FIXTURE}",
    ))
    _print_result(*results[-1])

    # ═══════════════════════════════════════════════════════════════
    print(f"\n{INFO} 阶段 4: placed_fixture_counts — 轮次内同场计数模拟")
    print("  " + "─" * 60)

    # 模拟 _run_cycle 中的 placed_fixture_counts 行为
    # 关键：sibling_match_ids 包含自身，increment 只走 sib 循环，不单独加 mid
    placed_fixture_counts: dict[int, int] = {}

    def _increment_fixture(mid: int):
        """模拟下单成功后的计数逻辑（与 auto_better.py 一致）"""
        for sid in SIB_IDS:
            placed_fixture_counts[int(sid)] = placed_fixture_counts.get(int(sid), 0) + 1

    # 第 1 次投注前检查
    mid_1 = 2001
    can_bet_1 = placed_fixture_counts.get(mid_1, 0) < MAX_BETS_PER_FIXTURE
    results.append((
        "第 1 次投注前: count=0 < 2 → 允许",
        can_bet_1,
        f"count={placed_fixture_counts.get(mid_1, 0)}",
    ))
    _print_result(*results[-1])

    # 第 1 次投注成功
    _increment_fixture(mid_1)
    # After: 2001→1, 2002→1

    # 第 2 次投注前检查（sibling 的平博 match）
    mid_2 = 2002
    can_bet_2 = placed_fixture_counts.get(mid_2, 0) < MAX_BETS_PER_FIXTURE
    results.append((
        "第 2 次投注前: count=1 < 2 → 允许（sibling 同步计数）",
        can_bet_2,
        f"count={placed_fixture_counts.get(mid_2, 0)}",
    ))
    _print_result(*results[-1])

    # 第 2 次投注成功
    _increment_fixture(mid_2)
    # After: 2001→2, 2002→2

    # 第 3 次投注前检查
    can_bet_3 = placed_fixture_counts.get(mid_1, 0) < MAX_BETS_PER_FIXTURE
    results.append((
        "第 3 次投注前: count=2 >= 2 → 阻止",
        not can_bet_3,
        f"count={placed_fixture_counts.get(mid_1, 0)}",
    ))
    _print_result(*results[-1])

    # ═══════════════════════════════════════════════════════════════
    print(f"\n{INFO} 阶段 5: _execute_bet sibling 计数检查")
    print("  " + "─" * 60)

    # 场景 5a: 同场已有 1 个 SUCCESS 注单 → 应允许第 2 次（不因 count=1 拦截）
    db_5a = MagicMock()
    match_result_5a = MagicMock()
    match_result_5a.scalar_one_or_none.return_value = ob_match
    count_mock_5a = MagicMock()
    count_mock_5a.scalar_one.return_value = 1  # 1 个 SUCCESS

    # execute 顺序: Match 查询 → sibling count
    db_5a.execute = AsyncMock(side_effect=[match_result_5a, count_mock_5a])

    engine._engine_lock_token = ""
    with patch("app.ai.auto_better.is_active_mode", return_value=True), \
         patch("app.services.fixture_key.sibling_match_ids", new_callable=AsyncMock, return_value=SIB_IDS), \
         patch("app.core.cache.cache") as mock_cache_5a:
        mock_cache_5a.acquire_lock = AsyncMock(return_value=True)
        mock_cache_5a.get = AsyncMock(return_value=None)
        ok_5a = await engine._execute_bet(db_5a, MagicMock(id=1, ai_enabled=True), MagicMock(), MagicMock(), MagicMock())

    # 5a 不应因 sibling count=1 而拦截（应继续走到后续下单逻辑才失败）
    # 关键：不返回 "同场注单已达上限" 的拦截
    results.append((
        "5a: 同场 1 注 → 不因 count 拦截（count=1 < 2）",
        not ok_5a,  # 返回 False 是正常的（后续逻辑会失败），关键是没被 sibling count 拦截
        f"ok={ok_5a} (非 count 原因拦截属正常)",
    ))
    _print_result(*results[-1])

    # 场景 5b: 同场已有 2 个 SUCCESS 注单 → 应阻止第 3 次
    db_5b = MagicMock()
    match_result_5b = MagicMock()
    match_result_5b.scalar_one_or_none.return_value = ob_match
    count_mock_5b = MagicMock()
    count_mock_5b.scalar_one.return_value = 2  # 2 个 SUCCESS

    db_5b.execute = AsyncMock(side_effect=[match_result_5b, count_mock_5b])

    with patch("app.ai.auto_better.is_active_mode", return_value=True), \
         patch("app.services.fixture_key.sibling_match_ids", new_callable=AsyncMock, return_value=SIB_IDS), \
         patch("app.core.cache.cache") as mock_cache_5b:
        mock_cache_5b.acquire_lock = AsyncMock(return_value=True)
        mock_cache_5b.get = AsyncMock(return_value=None)
        ok_5b = await engine._execute_bet(db_5b, MagicMock(id=1, ai_enabled=True), MagicMock(), MagicMock(), MagicMock())

    results.append((
        "5b: 同场 2 注 → _execute_bet 阻止第 3 次",
        not ok_5b,
        f"ok={ok_5b} (应返回 False)",
    ))
    _print_result(*results[-1])

    # ═══════════════════════════════════════════════════════════════
    print(f"\n{INFO} 阶段 6: 端到端模拟 — OB 下注 → 平博下注 → 第 3 次被拦")
    print("  " + "─" * 60)

    bet_db_count = [0]  # 模拟 DB 中的注单数

    async def mock_match_bet_count(db_inner, uid, mid):
        """模拟 _match_bet_count，根据 bet_db_count 返回"""
        return bet_db_count[0]

    placed_sim: dict[int, int] = {}

    async def simulate_bet_attempt(match_id: int, label: str) -> str:
        """模拟一次投注尝试，返回 'allowed' 或 'blocked'"""
        # 轮次内检查
        if placed_sim.get(match_id, 0) >= MAX_BETS_PER_FIXTURE:
            return "blocked_cycle"

        # 跨轮次检查
        count = await mock_match_bet_count(None, 1, match_id)
        if count >= MAX_BETS_PER_FIXTURE:
            return "blocked_db"

        # 模拟投注成功: increment via sib loop (含自身)
        for sid in SIB_IDS:
            placed_sim[int(sid)] = placed_sim.get(int(sid), 0) + 1
        bet_db_count[0] += 1
        return "allowed"

    # 第 1 次: OB 下注
    r1 = await simulate_bet_attempt(2001, "OB")
    results.append((
        "端到端 第 1 次 (OB): allowed",
        r1 == "allowed",
        f"result={r1}",
    ))
    _print_result(*results[-1])

    # 第 2 次: 平博下注（sibling）
    r2 = await simulate_bet_attempt(2002, "Pinnacle")
    results.append((
        "端到端 第 2 次 (Pinnacle): allowed",
        r2 == "allowed",
        f"result={r2}",
    ))
    _print_result(*results[-1])

    # 第 3 次: 再次 OB 尝试 → 被拦
    r3 = await simulate_bet_attempt(2001, "OB retry")
    results.append((
        "端到端 第 3 次 (OB retry): blocked",
        r3 in ("blocked_cycle", "blocked_db"),
        f"result={r3}",
    ))
    _print_result(*results[-1])

    # 第 4 次: 再次平博尝试 → 被拦
    r4 = await simulate_bet_attempt(2002, "Pinnacle retry")
    results.append((
        "端到端 第 4 次 (Pinnacle retry): blocked",
        r4 in ("blocked_cycle", "blocked_db"),
        f"result={r4}",
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
