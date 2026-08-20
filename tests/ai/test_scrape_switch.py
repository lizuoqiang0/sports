"""数据源开关（爬取控制）单元测试。

场景：用户点关闭后仍在爬取。根因 3 处：
1. 预取一轮耗时很长（批量 300 场），中途关闭无法中止在途轮次
2. AI 分析的 fetch_match_context(refresh_on_miss) 实时爬取不受开关控制
3. nowscore_prefetcher 循环只在轮开始检查开关

修复验证：
- 预取场间检查点（_parse_and_save 双重检查）
- fetch_match_context 缓存未命中时若开关关闭 → 不爬取
- _scrape_switch_enabled 辅助函数语义
"""
from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

PASS = "\033[32m✅\033[0m"
FAIL = "\033[31m❌\033[0m"


def test_prefetch_checkpoints():
    """修复1：预取场间检查点（双重检查：发起前 + 信号量内）。"""
    import app.services.nowscore_scraper as scr

    src = inspect.getsource(scr.prefetch_today_all_contexts)
    return [
        ("预取每场次发起前检查开关", "_is_enabled" in src, ""),
        ("信号量排队后二次检查（等待期间开关可能已关闭）",
         src.count("_is_enabled") >= 2, f"检查次数={src.count('_is_enabled')}"),
        ("检查失败不阻断（except pass 容错）", "except Exception:\n                    pass" in src or "except Exception:" in src, ""),
    ]


def test_fetch_context_gate():
    """修复2：AI 分析实时爬取受开关控制。"""
    import app.ai.match_context as mc

    src = inspect.getsource(mc.fetch_match_context)
    has_helper = hasattr(mc, "_scrape_switch_enabled")
    return [
        ("存在 _scrape_switch_enabled 辅助函数", has_helper, ""),
        ("缓存未命中后先查开关再决定爬取", "_scrape_switch_enabled()" in src, ""),
        ("开关关闭返回空上下文（带说明 note）",
         "数据源开关已关闭" in src, ""),
        ("缓存命中不受开关影响（开关只管新爬取）",
         src.find("if cached:") < src.find("_scrape_switch_enabled"), ""),
    ]


async def _test_switch_semantics_async():
    """_scrape_switch_enabled 语义：开=1/true/on，关=0/false。

    注：生产 cache.decode_responses=True，get 返回 str；
    FakeCache 直接返回 str 模拟真实行为（bytes 会触发 str(b'1')="b'1'" 陷阱）。
    """
    from unittest.mock import patch

    from app.ai.match_context import _scrape_switch_enabled

    class _FakeCache:
        def __init__(self, val):
            self.val = val

        async def get(self, key):
            return self.val

    results = []
    for val, expected in [("1", True), ("0", False), ("true", True),
                          ("false", False), ("True", True), (None, True),
                          ("on", True), ("off", False)]:
        with patch("app.core.cache.cache", _FakeCache(val)):
            got = await _scrape_switch_enabled()
        results.append((
            f"开关值 {val!r} → {expected}",
            got == expected,
            f"enabled={got}",
        ))
    return results


def test_prefetcher_loop():
    """修复3：预取循环轮开始检查（已有逻辑回归确认）。"""
    import app.services.nowscore_prefetcher as pf

    src = inspect.getsource(pf._prefetch_loop)
    return [
        ("循环每 tick 检查开关", "enabled = await _is_enabled()" in src, ""),
        ("开关关闭 → 跳过本轮（sleep tick）", "if not enabled" in src, ""),
        ("开关关闭 → 不写 last_result（不推进下次调度）",
         src.find("if not enabled") < src.find("_set_last_result"), ""),
    ]


def test_admin_switch_api():
    """开关 API 与底层键一致（前端开关 ↔ 爬取控制同键）。"""
    import app.api.admin as admin

    return [
        ("admin 开关键 = nowscore:prefetch:enabled", admin._SWITCH_KEY == "nowscore:prefetch:enabled", ""),
        ("toggle 写 Redis（运行时生效无需重启）", "cache.set(_SWITCH_KEY" in inspect.getsource(admin.toggle_nowscore_switch), ""),
    ]


async def run_all():
    results = []
    results.extend(test_prefetch_checkpoints())
    results.extend(test_fetch_context_gate())
    results.extend(await _test_switch_semantics_async())
    results.extend(test_prefetcher_loop())
    results.extend(test_admin_switch_api())
    return results


def main():
    results = asyncio.run(run_all())
    print("\n" + "=" * 64)
    print("数据源开关（爬取控制）测试")
    print("=" * 64)
    fails = 0
    for name, ok, detail in results:
        print(f"{PASS if ok else FAIL}  {name}")
        if detail:
            print(f"       {detail}")
        fails += 0 if ok else 1
    print("-" * 64)
    print(f"通过 {len(results) - fails}/{len(results)}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
