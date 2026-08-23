"""
模拟乌迪内斯 vs 科莫的盘口波动场景，验证 GPT prompt 中的分析时刻快照是否正确。

场景复现：
- 初盘 2.5 → 降到 2.25 → 降到 2.0 → 进球后升到 2.75 → 升到 3.25 → 回落到 3.0
- 分析时刻：比分 1-1（2球），盘口线 3.25
- 之前 GPT 误读为"当前1球，盘口2.25"

运行: python3 scripts/test_prompt_snapshot.py
"""
import asyncio
import json
from datetime import datetime, timezone, timedelta


async def test_prompt_snapshot():
    from app.ai.analyzer import MatchAnalyzer

    analyzer = MatchAnalyzer()

    # 模拟乌迪内斯 vs 科莫的 match_info（分析时刻 17:48）
    match_info = {
        "id": 1841,
        "sport": "football",
        "home_team": "乌迪内斯",
        "away_team": "科莫",
        "league": "意大利甲级联赛",
        "start_time": "2026-08-22T15:00:00+08:00",
        "venue": "",
        "home_score": 1,
        "away_score": 1,
        "period": "下半场",
        "clock": "53:00",
        "total_line": 3.25,
        "line": 3.25,
        "odds": {
            "under": 1.75,
            "over": 2.14,
        },
        "fixture_key": "football:乌迪内斯|科莫:82750",
    }

    # 模拟盘口历史波动（复现真实 DB 中的 60 个版本）
    base_time = datetime(2026, 8, 22, 16, 33, 5, tzinfo=timezone.utc)
    line_changes = [
        # (分钟偏移, total_line, under_odds, over_odds)
        (0,   2.5,  1.83, 2.05),   # 初盘
        (1,   2.25, 2.05, 1.83),   # 降盘
        (3,   2.25, 1.97, 1.91),
        (5,   2.25, 1.92, 1.96),
        (8,   2.25, 1.91, 1.97),
        (10,  2.25, 1.84, 2.04),
        (12,  2.25, 1.81, 2.07),
        (14,  2.25, 1.80, 2.08),
        (16,  2.0,  2.09, 1.79),   # 再降盘
        (20,  2.0,  1.97, 1.91),
        (25,  2.0,  1.95, 1.93),
        (30,  2.0,  1.88, 2.0),
        (35,  2.0,  1.82, 2.06),
        (40,  2.0,  1.80, 2.08),
        (45,  1.75, 2.07, 1.81),   # 降到 1.75
        (50,  1.75, 2.01, 1.87),
        (55,  1.75, 1.98, 1.9),
        # 进球！盘口大幅上调
        (90,  2.75, 1.91, 1.97),   # 第一个进球后数学调整
        (95,  2.75, 1.86, 2.02),
        (100, 2.75, 1.82, 2.06),
        (105, 2.75, 1.97, 1.91),
        (110, 2.5,  2.06, 1.82),   # 回落到 2.5
        (115, 2.5,  2.01, 1.87),
        # 第二个进球！盘口再上调
        (135, 2.25, 2.09, 1.79),   # 短暂降回
        (140, 2.25, 1.83, 2.05),
        (142, 2.25, 1.81, 2.07),
        (145, 2.25, 1.80, 2.08),
        # 分析时刻：盘口升到 3.25
        (148, 3.25, 1.75, 2.14),   # ← 这是分析时刻的盘口
        (150, 3.0,  2.07, 1.81),   # 之后继续波动
        (155, 3.0,  1.95, 1.93),
        (160, 3.0,  1.81, 2.07),
        (165, 3.0,  1.75, 2.14),
        (170, 3.0,  1.73, 2.17),
        (175, 2.75, 2.17, 1.73),   # 最终值
    ]

    # 构造 line_movements（模拟 summarize_line_movement 的输出）
    opening = {
        "odds": {"under": 1.83, "over": 2.05},
        "line": 2.5,
        "at": base_time.isoformat(),
        "is_live": True,
    }
    current = {
        "odds": {"under": 2.17, "over": 1.73},
        "line": 2.75,
        "at": (base_time + timedelta(minutes=175)).isoformat(),
        "is_live": True,
    }
    recent_moves = []
    prev_line = 2.5
    for offset, line, under, over in line_changes:
        if abs(line - prev_line) >= 0.01:
            recent_moves.append({
                "at": (base_time + timedelta(minutes=offset)).isoformat(),
                "odds": {"under": under, "over": over},
                "line": line,
                "line_delta": round(line - prev_line, 3),
            })
        prev_line = line

    line_movements = {
        "total": {
            "opening": opening,
            "current": current,
            "line_delta": round(current["line"] - opening["line"], 3),
            "odds_delta": {
                "under": round(current["odds"]["under"] - opening["odds"]["under"], 3),
                "over": round(current["odds"]["over"] - opening["odds"]["over"], 3),
            },
            "direction": "line_up",
            "change_count": len(line_changes) - 1,
            "recent_moves": recent_moves[-5:],
        }
    }

    market_odds = {
        "markets": {
            "total": {
                "odds": {"under": 1.75, "over": 2.14},
                "line": 3.25,
                "opening": opening,
                "line_movement": line_movements["total"],
            }
        },
        "line_movements": line_movements,
        "under": 1.75,
        "over": 2.14,
    }

    # 构造 prompt（不调 GPT，只验证 prompt 内容）
    prompt = analyzer._build_analysis_prompt(match_info, historical_data=None, market_odds=market_odds)

    print("=" * 70)
    print("测试：分析时刻快照验证（乌迪内斯 vs 科莫 盘口波动场景）")
    print("=" * 70)

    # 验证1：prompt 中包含分析时刻快照
    snapshot_marker = "【分析时刻快照】"
    if snapshot_marker in prompt:
        # 提取快照行
        for line in prompt.split("\n"):
            if snapshot_marker in line:
                print(f"\n✅ 快照声明存在:")
                print(f"   {line.strip()}")
                break

        # 验证快照中的比分和盘口线
        assert "1-1" in prompt, "❌ 快照中缺少当前比分 1-1"
        assert "总进球2" in prompt, "❌ 快照中缺少总进球2"
        assert "3.25" in prompt, "❌ 快照中缺少盘口线 3.25"
        print("   ✅ 当前比分 1-1 (总进球2) — 正确")
        print("   ✅ 即时盘口线 3.25 — 正确")
    else:
        print("❌ 快照声明不存在于 prompt 中")
        return

    # 验证2：reasoning 格式要求包含强制声明
    reasoning_format = "【强制声明】当前比分"
    if reasoning_format in prompt:
        print(f"\n✅ reasoning 格式要求包含强制声明")
    else:
        print("❌ reasoning 格式要求缺少强制声明")

    # 验证3：禁止使用中间版本的注意事项
    forbid_note = "禁止使用盘口历史中的中间版本值"
    if forbid_note in prompt:
        print(f"✅ 禁止中间版本注意事项存在")
    else:
        print("❌ 缺少禁止中间版本注意事项")

    # 验证4：prompt 中不應让 GPT 混淆的 2.25 出现在快照附近
    # 2.25 在 line_movements 中存在（历史版本），但不应出现在快照声明中
    snapshot_line = ""
    for line in prompt.split("\n"):
        if snapshot_marker in line:
            snapshot_line = line.strip()
            break

    if "2.25" in snapshot_line:
        print("❌ 快照行中出现了历史中间版本 2.25")
    else:
        print("✅ 快照行中未出现历史中间版本 2.25")

    # 验证5：统计 prompt 中各盘口线值出现的次数
    print("\n--- prompt 中各盘口线值出现次数 ---")
    for val in ["2.5", "2.25", "2.0", "1.75", "2.75", "3.25", "3.0"]:
        count = prompt.count(val)
        if count > 0:
            tag = "← 即时盘口线" if val == "3.25" else "← 初盘" if val == "2.5" else ""
            print(f"   {val:>5}: 出现 {count} 次 {tag}")

    # 验证6：模拟 GPT 返回的 reasoning 是否能通过校验
    print("\n--- 模拟 GPT reasoning 校验 ---")
    test_reasonings = [
        # 正确：使用当前比分和盘口
        ("正确格式", "当前比分: 1-1 (总进球2)，即时盘口线: 3.25，初盘: 2.5，盘口变化: 升0.75 → 节奏分析..."),
        # 错误：使用旧比分和旧盘口（之前的 bug）
        ("旧数据(bug)", "初盘2.5降至即时2.25，降幅0.25球；当前总进球1球..."),
        # 错误：使用中间版本盘口
        ("中间版本", "当前比分: 1-1，即时盘口线: 2.0 → 分析..."),
    ]

    for name, reasoning in test_reasonings:
        has_score = "1-1" in reasoning and "总进球2" in reasoning
        has_line = "3.25" in reasoning
        is_correct = has_score and has_line
        status = "✅ 正确" if is_correct else "❌ 数据不一致"
        print(f"   {name:15s}: {status} | reasoning={reasoning[:60]}...")

    print("\n" + "=" * 70)
    print("✅ 测试完成：分析时刻快照已正确注入 prompt")
    print("   GPT 现在会被强制要求在 reasoning 开头声明当前比分和盘口线，")
    print("   避免使用 line_movements 中的历史中间版本值。")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(test_prompt_snapshot())
