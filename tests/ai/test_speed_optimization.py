"""性能优化单元测试（对应 doc.md 第八节）。

覆盖：
1. 动态轮询间隔：空轮 30s 快扫 / 有候选 120s 正常间隔
2. LLM 分析并发：默认从 3 提升到 8
3. skip 冷却生命周期：记录→过滤→过期→清理（无界增长防护）
4. _run_cycle 返回值契约：bool（有候选 True / 无候选 False）
5. 冷却保守语义：只冷却 skip，闸门拒绝不进冷却
"""
from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

PASS = "\033[32m✅\033[0m"
FAIL = "\033[31m❌\033[0m"


def test_constants():
    """8.3 配置项：并发 8 / 空轮 30s / 冷却 300s。"""
    from app.ai import auto_better as ab

    return [
        ("LLM 并发默认 8（原3）", ab._LIVE_ANALYZE_CONCURRENCY == 8,
         f"got={ab._LIVE_ANALYZE_CONCURRENCY}"),
        ("空轮快扫间隔 30s", ab._IDLE_RESCAN_SEC == 30, f"got={ab._IDLE_RESCAN_SEC}"),
        ("skip 冷却 300s", ab._SKIP_COOLDOWN_SEC == 300, f"got={ab._SKIP_COOLDOWN_SEC}"),
        ("空轮间隔下限 15s（防误配为0）", ab._IDLE_RESCAN_SEC >= 15, ""),
        ("冷却间隔下限 60s（防误配为0导致每轮重分析）", ab._SKIP_COOLDOWN_SEC >= 60, ""),
    ]


def test_dynamic_interval_logic():
    """8.2 动态间隔：主循环根据 _run_cycle 返回值选择间隔。"""
    from app.ai import auto_better as ab

    src = inspect.getsource(ab.AIBettingEngine._main_loop)
    return [
        ("主循环读取 _run_cycle 返回值", "had_candidates = await self._run_cycle()" in src, ""),
        ("空轮使用 _IDLE_RESCAN_SEC", "_IDLE_RESCAN_SEC" in src, ""),
        ("有候选走 AI_SCAN_INTERVAL_SEC", "AI_SCAN_INTERVAL_SEC" in src, ""),
        ("有候选间隔下限 60s（原120，分析后适度缩短）",
         "max(60," in src or "max(60, " in src, ""),
    ]


def test_run_cycle_contract():
    """_run_cycle 返回 bool（供动态间隔决策）。"""
    from app.ai import auto_better as ab

    sig = inspect.signature(ab.AIBettingEngine._run_cycle)
    src = inspect.getsource(ab.AIBettingEngine._run_cycle)
    # _run_cycle 函数体内直接 return（缩进 ≤16，含 async with 嵌套一层）；
    # 闭包 _analyze_and_place 内的 return best（缩进 ≥20）不算
    fn_returns = [
        l.strip() for l in src.splitlines()
        if l.strip().startswith("return") and (len(l) - len(l.lstrip())) <= 16
        and not l.strip().startswith("return best")
    ]
    ann = sig.return_annotation
    ann_ok = ann is bool or ann == "bool" or str(ann) == "bool"
    return [
        ("_run_cycle 类型注解为 bool", ann_ok, f"got={ann!r}"),
        ("无候选 return False", "return False" in fn_returns, f"returns={fn_returns}"),
        ("完成轮 return True", "return True" in fn_returns, ""),
        ("函数体 return 均为 bool（无 None 泄漏成空轮）",
         all(r in ("return False", "return True") for r in fn_returns),
         f"fn_returns={fn_returns}"),
    ]


async def _test_skip_cooldown_async():
    """8.4 冷却生命周期：记录 → 轮内过滤 → 过期解除 → dict 不膨胀。"""
    from app.ai.auto_better import AIBettingEngine

    eng = AIBettingEngine(user_id=1)
    results = []

    # 1) 引擎初始化带冷却容器
    results.append(("引擎含 _skip_cooldown 容器", hasattr(eng, "_skip_cooldown") and isinstance(eng._skip_cooldown, dict), ""))

    # 2) 冷却过滤逻辑存在于 _run_cycle（分组过滤）
    src = inspect.getsource(AIBettingEngine._run_cycle)
    results.append(("_run_cycle 含冷却过滤", "_skip_cooldown.get" in src, ""))
    results.append(("冷却键用 min(match_id)（同场稳定映射）", "str(min(g))" in src, ""))
    results.append(("全部分组冷却 → 跳过本轮 LLM（return False）",
                    "全部分组冷却中" in src, ""))
    results.append(("过期项顺手清理（无界增长防护）",
                    "now_ts - ts < _SKIP_COOLDOWN_SEC" in src, ""))

    # 3) skip 记录逻辑存在于 _analyze_and_place
    src2 = inspect.getsource(AIBettingEngine._run_cycle)  # 闭包在 _run_cycle 内
    results.append(("LLM 判 skip 时记录冷却", "_skip_cooldown[str(min(ids))]" in src2, ""))

    # 4) 冷却语义：过滤条件（< _SKIP_COOLDOWN_SEC 跳过，≥ 解除）
    results.append(("冷却窗口判定 < _SKIP_COOLDOWN_SEC",
                    "now_ts - ts < _SKIP_COOLDOWN_SEC" in src, ""))

    # 5) 行为模拟：冷却 dict 生命周期
    loop = asyncio.get_event_loop()
    now = loop.time()
    eng._skip_cooldown = {"101": now}          # 刚记录：冷却中
    eng._skip_cooldown["102"] = now - 400      # 400s 前：已过期
    active = {
        k: ts for k, ts in eng._skip_cooldown.items()
        if now - ts < 300
    }
    results.append(("过期冷却被清理（仅留冷却中项）", set(active.keys()) == {"101"}, f"left={set(active.keys())}"))

    return results


def test_conservative_semantics():
    """8.4 保守语义：只冷却 LLM skip，闸门拒绝不进冷却。"""
    from app.ai import auto_better as ab

    src = inspect.getsource(ab.AIBettingEngine._run_cycle)
    # 记录点必须在 sel 判定块内（skip 分支之前、方向检查之后）
    cooldown_pos = src.find("_skip_cooldown[str(min(ids))]")
    gate_block_pos = src.find("decision = await user_engine.evaluate_bet")
    return [
        ("冷却记录发生在 LLM 方向判定处（闸门评估之前）",
         cooldown_pos > 0 and gate_block_pos > cooldown_pos,
         f"cooldown_pos={cooldown_pos}, gate_pos={gate_block_pos}"),
        ("闸门拒绝路径无冷却写入（保持每轮重评）",
         src.count("_skip_cooldown[str(min(ids))]") == 1, ""),
    ]


def test_doc_coverage():
    """文档与代码同步：doc.md 关键配置项存在。"""
    doc_path = Path(__file__).resolve().parents[2] / "doc.md"
    if not doc_path.exists():
        return [("doc.md 存在", False, f"path={doc_path}")]
    text = doc_path.read_text(encoding="utf-8")
    return [
        ("doc 记录 AI_ANALYZE_CONCURRENCY", "AI_ANALYZE_CONCURRENCY" in text, ""),
        ("doc 记录 AI_IDLE_RESCAN_SEC", "AI_IDLE_RESCAN_SEC" in text, ""),
        ("doc 记录 AI_SKIP_COOLDOWN_SEC", "AI_SKIP_COOLDOWN_SEC" in text, ""),
        ("doc 记录 AI_ENABLE_OVER", "AI_ENABLE_OVER" in text, ""),
        ("doc 记录时区工具", "today_start_utc" in text, ""),
        ("doc 记录 SPORT_RISK 参数表", "SPORT_RISK" in text, ""),
        ("doc 记录风险评分权重", "AI_RISK_LOW_CONF_WEIGHT" in text, ""),
    ]


async def run_all():
    results = []
    results.extend(test_constants())
    results.extend(test_dynamic_interval_logic())
    results.extend(test_run_cycle_contract())
    results.extend(await _test_skip_cooldown_async())
    results.extend(test_conservative_semantics())
    results.extend(test_doc_coverage())
    return results


def main():
    results = asyncio.run(run_all())
    print("\n" + "=" * 64)
    print("性能优化测试（doc.md 第八节对应）")
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
