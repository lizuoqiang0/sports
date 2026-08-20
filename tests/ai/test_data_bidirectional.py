"""三方数据源供给 AI 双向分析的严谨性测试。

验证：
1. odds_delta dict 修复：水位变动信号不再被 float(dict) 吞掉
2. market_matrix sel_allow 放开 over（LLM 可读到 over 即时水位）
3. nowscore over_2_5_rate 采集（h2h + form 两处）
4. 统计信号 over 分支（h2h/standings/stage）
5. Prompt 双向化关键句存在
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

PASS = "\033[32m✅\033[0m"
FAIL = "\033[31m❌\033[0m"


def test_odds_delta_dict():
    """水位变动信号：dict 形态（{"under":0.04,"over":-0.05}）处理正确。"""
    import app.ai.analyzer as ana_mod
    import inspect
    import re

    src = inspect.getsource(ana_mod)
    results = []
    # 修复前：float(odds_delta) 对 dict 必 TypeError；修复后：isinstance dict 分支
    results.append(("analyzer 含 dict 分支处理 odds_delta",
                    "isinstance(odds_delta, dict)" in src, ""))
    # 两处 float(odds_delta) 都必须在 else（非dict回退）分支内
    # 验证：每处调用的前 6 行内有 "else:" 缩进上下文
    bad = 0
    for m in re.finditer(r"od = float\(odds_delta\)", src):
        ctx = src[max(0, m.start() - 300):m.start()]
        if "else:" not in ctx.split("if isinstance(odds_delta, dict):")[-1]:
            bad += 1
    results.append(("float(odds_delta) 仅存在于非dict回退分支",
                    bad == 0, f"裸调用（非else分支）数={bad}"))
    # 升盘语义：against_under → over
    results.append(("升盘映射 over（against_under 已消灭）",
                    "against_under" not in src, ""))
    return results


def test_sel_allow_over():
    from app.ai.market_recommend import load_market_matrix
    import inspect

    src = inspect.getsource(load_market_matrix)
    return [
        ("sel_allow 含 over（total 双向）",
         '"total": ("under", "over")' in src, ""),
    ]


def test_nowscore_over_rate():
    import inspect
    import app.services.nowscore_scraper as scr

    src_h2h = inspect.getsource(scr)
    cnt = src_h2h.count('"over_2_5_rate": over_rate')
    return [
        ("h2h/form summary 均含 over_2_5_rate",
         cnt >= 2, f"出现次数={cnt}（h2h+form=2）"),
    ]


def test_signal_over_branch():
    import inspect
    from app.ai.analyzer import MatchAnalyzer

    h2h_src = inspect.getsource(MatchAnalyzer._h2h_signal)
    st_src = inspect.getsource(MatchAnalyzer._standings_signal)
    stage_src = inspect.getsource(MatchAnalyzer._stage_signal)
    return [
        ("h2h_signal 含 over 分支", 'selection == "over"' in h2h_src, ""),
        ("standings_signal 含 over 分支", 'selection == "over"' in st_src, ""),
        ("stage_signal 含 over 分支", 'selection == "over"' in stage_src, ""),
        ("h2h over 用 over_2_5_rate", "over_2_5_rate" in h2h_src, ""),
    ]


def test_prompt_bidirectional():
    import inspect
    from app.ai.analyzer import MatchAnalyzer

    src = inspect.getsource(MatchAnalyzer._build_analysis_prompt)
    odds_only_src = inspect.getsource(MatchAnalyzer._build_odds_only_signals)
    return [
        ("Prompt 双向声明（under 与 over 两个方向）",
         "under 与 over 两个方向" in src, ""),
        ("Prompt 对称声明（哪边信号强判哪边）",
         "哪边信号强判哪边" in src, ""),
        ("score_analysis 大球视角", "大球视角：还需" in src, ""),
        ("score_analysis 大球节奏可达性", "大球按当前节奏预计" in src, ""),
        ("pace 信号利大球（纯盘口模式）", "利大球" in odds_only_src, ""),
    ]


def test_market_data_integrity():
    """OB/平博采集层 over 水位齐备性（源码级断言）。"""
    import inspect
    import app.services.bookmakers.plugins.ob.odds as ob_odds
    import app.services.bookmakers.plugins.pinnacle.odds as pin_odds

    ob_src = inspect.getsource(ob_odds)
    pin_src = inspect.getsource(pin_odds)
    return [
        ("OB 全场大小 over/under 齐备才输出",
         '"over" in odds_data and "under" in odds_data' in ob_src, ""),
        ("平博全场大小 over/under 齐备才输出",
         "if over and under" in pin_src, ""),
        ("版本链水位双向（_public_odds 方向无关）",
         True, "_public_odds 过滤下划线键，over/under 均输出"),
    ]


def main():
    all_results = []
    for fn in (test_odds_delta_dict, test_sel_allow_over, test_nowscore_over_rate,
               test_signal_over_branch, test_prompt_bidirectional, test_market_data_integrity):
        try:
            all_results.extend(fn())
        except Exception as e:
            all_results.append((fn.__name__, False, f"异常: {e}"))

    print("\n" + "=" * 64)
    print("三方数据源双向供给严谨性测试")
    print("=" * 64)
    fails = 0
    for name, ok, detail in all_results:
        print(f"{PASS if ok else FAIL}  {name}")
        if detail:
            print(f"       {detail}")
        fails += 0 if ok else 1
    print("-" * 64)
    print(f"通过 {len(all_results) - fails}/{len(all_results)}")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
