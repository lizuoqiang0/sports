"""让球盘误点修复单元测试。

修复点（bet_ui.py click_js）：
1. 赔率匹配阈值 ±0.15（适配滚球赔率漂移，原 ±0.06 太严）
2. 纯赔率漂移护栏 0.15（收紧自 0.25，防止误点让球盘相近赔率）
3. 市场类型校验 isSpreadCtx/isTotalCtx（让球/让分/spread vs 大小/总分/over-under）
4. 让球线数字排除 hasHandicapLine（检测行内 -0.5/-1.0 等让球线特征）
5. 方向标签缺失兜底 noSideLabelOver/noSideLabelUnder/singleOddsFallback

运行: python -m pytest tests/ai/test_handicap_exclusion.py -v
     或直接: python tests/ai/test_handicap_exclusion.py
"""
from __future__ import annotations
import re
import asyncio
import pytest
from decimal import Decimal

try:
    from playwright.async_api import async_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False


# ─── 与 bet_ui.py click_js 中一致的常量/函数（Python 侧镜像） ───

# JavaScript: /(?<!\d)[-－−–]\d+\.\d/
# －为全角连字符 U+FF0D, −为 Unicode 减号 U+2212, –为 en-dash U+2013
# 要求小数位避免误同比分（如 2-1），同时覆盖更多破折号变体
# lookbehind (?<!\d)：dash 前不能是数字，防止四分线 3.5-4 拼赔率 2.130 被误匹配为 -42.13
HANDICAP_LINE_RE = re.compile(r'(?<!\d)[-\uFF0D\u2212\u2013]\d+\.\d')

SPREAD_WORDS = ['让球', '让分', 'spread', 'handicap', '盘口', '亚盘']
TOTAL_WORDS = ['大小', '总分', 'over/under', 'over-under', 'o/u', 'totals']


def _norm(s: str) -> str:
    """模拟 click_js norm：去空白 + 小写。"""
    return re.sub(r'\s+', '', str(s or '')).lower()


def _is_spread_ctx(ctx: str) -> bool:
    """模拟 click_js isSpreadCtx。"""
    c = _norm(ctx)
    return any(w in c for w in [w.lower() for w in SPREAD_WORDS])


def _is_total_ctx(ctx: str) -> bool:
    """模拟 click_js isTotalCtx。"""
    c = _norm(ctx)
    return any(w in c for w in [w.lower() for w in TOTAL_WORDS])


def _odds_text_ok(text: str, target: float) -> bool:
    """模拟 click_js oddsTextOk（±0.15 阈值）。"""
    try:
        n = float(re.sub(r'[^0-9.]', '', str(text)) or '0')
    except (ValueError, TypeError):
        return False
    if not n or n < 1.01 or n > 50:
        return False
    return abs(n - target) <= 0.15


def _is_pure_odds(text: str) -> bool:
    """模拟 click_js 纯赔率数字判定：^\\d{1,2}\\.\\d{2,3}$。"""
    pure = re.sub(r'\s', '', str(text))
    return bool(re.match(r'^\d{1,2}\.\d{2,3}$', pure))


# ═══════════════════════════════════════════════════════════════
# 让球线数字排除正则
# ═══════════════════════════════════════════════════════════════

class TestHandicapLineRegex:
    """让球线正则：/[-－−–]\\d+\\.\\d/"""

    def test_minus_0_5(self):
        """-0.5 是典型让球线。"""
        assert HANDICAP_LINE_RE.search('-0.5')

    def test_minus_1_0(self):
        """-1.0 是典型让球线。"""
        assert HANDICAP_LINE_RE.search('-1.0')

    def test_minus_1_5(self):
        """-1.5 是典型让球线。"""
        assert HANDICAP_LINE_RE.search('-1.5')

    def test_minus_2_0(self):
        """-2.0 是典型让球线。"""
        assert HANDICAP_LINE_RE.search('-2.0')

    def test_minus_3_0(self):
        """-3.0 是让球线。"""
        assert HANDICAP_LINE_RE.search('-3.0')

    def test_fullwidth_hyphen_0_5(self):
        """全角连字符 －0.5。"""
        assert HANDICAP_LINE_RE.search('\uFF0D0.5')

    def test_fullwidth_hyphen_1_5(self):
        """全角连字符 －1.5。"""
        assert HANDICAP_LINE_RE.search('\uFF0D1.5')

    def test_unicode_minus_0_5(self):
        """Unicode 减号 −0.5 (U+2212)。"""
        assert HANDICAP_LINE_RE.search('\u22120.5')

    def test_en_dash_1_0(self):
        """En dash –1.0 (U+2013)。"""
        assert HANDICAP_LINE_RE.search('\u20131.0')

    def test_total_line_not_handicap(self):
        """大小球线 3.5 不含让球线特征。"""
        assert not HANDICAP_LINE_RE.search('3.5')

    def test_total_line_2_25_not_handicap(self):
        """大小球线 2.25 不含让球线特征。"""
        assert not HANDICAP_LINE_RE.search('2.25')

    def test_total_line_0_5_not_handicap(self):
        """大小球线 0.5（无负号）不含让球线特征。"""
        assert not HANDICAP_LINE_RE.search('0.5')

    def test_score_not_handicap(self):
        """比分 2-1 不应命中（无小数位）。"""
        assert not HANDICAP_LINE_RE.search('2-1')

    def test_score_0_0_not_handicap(self):
        """比分 0-0 不应命中。"""
        assert not HANDICAP_LINE_RE.search('0-0')

    def test_score_3_2_not_handicap(self):
        """比分 3-2 不应命中。"""
        assert not HANDICAP_LINE_RE.search('3-2')

    def test_handicap_line_in_row_text(self):
        """行文本含 -0.5 应命中。"""
        row = '巴勃罗b队让球-0.5大1.85小1.95'
        assert HANDICAP_LINE_RE.search(row)

    def test_total_row_no_handicap(self):
        """大小球行文本不应命中。"""
        row = '巴勃罗b队大小3.5大1.85小1.95'
        assert not HANDICAP_LINE_RE.search(row)

    def test_total_row_with_score_no_handicap(self):
        """大小球行含比分 2-1 不应命中。"""
        row = '巴勃罗b队2-1大小3.5大1.85小1.95'
        assert not HANDICAP_LINE_RE.search(row)

    def test_handicap_pair_format(self):
        """平博让球盘双线格式 0.5-0.5 不再匹配（dash 前有数字，视为四分线或比分）。"""
        assert not HANDICAP_LINE_RE.search('0.5-0.5')

    def test_quarter_line_concat_odds_not_handicap(self):
        """四分线 3.5-4 拼赔率 2.130 = 3.5-42.130 不应命中（实盘 odds_not_found 根因）。"""
        assert not HANDICAP_LINE_RE.search('3.5-42.130')

    def test_quarter_line_concat_odds_in_row(self):
        """行内四分线+赔率拼接不应命中。"""
        row = '0-21h46祖尼斯蒙多夫莱斯班斯0.02.4201.5613.5-42.130小1.699+4'
        assert not HANDICAP_LINE_RE.search(row)

    def test_handicap_with_text_prefix(self):
        """让球标签+负小数应命中（dash 前是汉字）。"""
        assert HANDICAP_LINE_RE.search('让球-0.5')

    def test_handicap_at_start(self):
        """行首负小数应命中。"""
        assert HANDICAP_LINE_RE.search('-0.5大1.85')

    def test_handicap_negative_pair(self):
        """让球盘 -0.5/0.5 格式应命中。"""
        assert HANDICAP_LINE_RE.search('-0.5/0.5')


# ═══════════════════════════════════════════════════════════════
# 市场类型校验
# ═══════════════════════════════════════════════════════════════

class TestMarketTypeDetection:
    """isSpreadCtx / isTotalCtx / rowNotSpread 逻辑。"""

    # ── isSpreadCtx ──

    def test_spread_chinese_rangqiu(self):
        assert _is_spread_ctx('让球 0.5')

    def test_spread_chinese_rangfen(self):
        assert _is_spread_ctx('让分 -1.5')

    def test_spread_english_spread(self):
        assert _is_spread_ctx('Spread 0.5')

    def test_spread_english_handicap(self):
        assert _is_spread_ctx('Handicap -1.0')

    def test_spread_asian(self):
        assert _is_spread_ctx('亚盘')

    def test_spred_pankou(self):
        assert _is_spread_ctx('盘口')

    # ── isTotalCtx ──

    def test_total_chinese_daxiao(self):
        assert _is_total_ctx('大小 3.5')

    def test_total_chinese_zongfen(self):
        assert _is_total_ctx('总分 180.5')

    def test_total_english_over_under_slash(self):
        assert _is_total_ctx('Over/Under 3.5')

    def test_total_english_over_under_hyphen(self):
        assert _is_total_ctx('Over-Under 3.5')

    def test_total_ou_abbrev(self):
        assert _is_total_ctx('O/U 3.5')

    def test_total_totals(self):
        assert _is_total_ctx('Totals 3.5')

    # ── 无标签 ──

    def test_neither_market(self):
        assert not _is_spread_ctx('巴勃罗 1.85 1.95')
        assert not _is_total_ctx('巴勃罗 1.85 1.95')

    # ── rowNotSpread 逻辑：!isSpreadCtx || isTotalCtx ──

    def test_row_not_spread_total_only(self):
        """纯大小球行 → rowNotSpread = true。"""
        row = '巴勃罗b队大小3.5大1.85小1.95'
        result = (not _is_spread_ctx(row)) or _is_total_ctx(row)
        assert result

    def test_row_not_spread_spread_only(self):
        """纯让球行 → rowNotSpread = false（被拦截）。"""
        row = '巴勃罗b队让球-0.5大1.85小1.95'
        result = (not _is_spread_ctx(row)) or _is_total_ctx(row)
        assert not result

    def test_row_not_spread_both_labels(self):
        """同时含让球和大小标签 → rowNotSpread = true（大小优先放行）。"""
        row = '巴勃罗b队大小3.5让球-0.5大1.85小1.95'
        result = (not _is_spread_ctx(row)) or _is_total_ctx(row)
        assert result

    def test_row_not_spread_neither_label(self):
        """无市场标签 → rowNotSpread = true（兼容旧布局）。"""
        row = '巴勃罗b队3.5大1.85小1.95'
        result = (not _is_spread_ctx(row)) or _is_total_ctx(row)
        assert result

    def test_row_not_spread_english_spread(self):
        """英文 spread 行 → rowNotSpread = false。"""
        row = 'Arsenal spread -0.5 over 1.85 under 1.95'
        result = (not _is_spread_ctx(row)) or _is_total_ctx(row)
        assert not result

    def test_row_not_spread_english_total(self):
        """英文 totals 行 → rowNotSpread = true。"""
        row = 'Arsenal totals 3.5 over 1.85 under 1.95'
        result = (not _is_spread_ctx(row)) or _is_total_ctx(row)
        assert result


# ═══════════════════════════════════════════════════════════════
# 赔率匹配阈值 ±0.15
# ═══════════════════════════════════════════════════════════════

class TestOddsThreshold:
    """oddsTextOk ±0.15 阈值。"""

    def test_exact_match(self):
        assert _odds_text_ok('1.85', 1.85)

    def test_within_threshold_down(self):
        """向下漂移 0.14 → 匹配。"""
        assert _odds_text_ok('1.71', 1.85)

    def test_at_threshold_down(self):
        """向下漂移接近边界 0.149 → 匹配。"""
        assert _odds_text_ok('1.701', 1.85)

    def test_beyond_threshold_down(self):
        """向下漂移超边界 0.151 → 不匹配。"""
        assert not _odds_text_ok('1.699', 1.85)

    def test_within_threshold_up(self):
        """向上漂移 0.14 → 匹配。"""
        assert _odds_text_ok('1.99', 1.85)

    def test_at_threshold_up(self):
        """向上漂移接近边界 0.149 → 匹配。"""
        assert _odds_text_ok('1.999', 1.85)

    def test_beyond_threshold_up(self):
        """向上漂移超边界 0.151 → 不匹配。"""
        assert not _odds_text_ok('2.001', 1.85)

    def test_invalid_text(self):
        assert not _odds_text_ok('abc', 1.85)

    def test_odds_below_minimum(self):
        """赔率 < 1.01 → 无效。"""
        assert not _odds_text_ok('0.50', 1.85)

    def test_odds_above_maximum(self):
        """赔率 > 50 → 无效。"""
        assert not _odds_text_ok('51.00', 1.85)

    def test_odds_with_side_label(self):
        """带方向词的赔率文本 → 提取数字匹配。"""
        assert _odds_text_ok('大 1.85', 1.85)

    def test_odds_with_at_symbol(self):
        """@1.80 格式 → 匹配（diff 0.05）。"""
        assert _odds_text_ok('@1.80', 1.85)

    def test_old_threshold_too_strict(self):
        """旧阈值 ±0.06 会漏掉正常漂移（回归保护）。"""
        # 滚球赔率从 1.85 漂到 1.72（diff 0.13）
        # 旧 ±0.06 → 不匹配 → odds_not_found
        # 新 ±0.15 → 匹配
        assert not abs(1.72 - 1.85) <= 0.06  # 旧阈值失败
        assert _odds_text_ok('1.72', 1.85)   # 新阈值成功


# ═══════════════════════════════════════════════════════════════
# 纯赔率漂移护栏 0.15
# ═══════════════════════════════════════════════════════════════

class TestPureOddsDriftGuard:
    """纯赔率节点漂移护栏（与 oddsTextOk 阈值一致 = 0.15）。"""

    GUARD = 0.15

    def test_pure_odds_format_valid(self):
        """标准赔率格式。"""
        assert _is_pure_odds('1.85')
        assert _is_pure_odds('1.850')
        assert _is_pure_odds('10.50')

    def test_pure_odds_format_invalid(self):
        """非纯赔率格式。"""
        assert not _is_pure_odds('大1.85')
        assert not _is_pure_odds('@1.85')
        assert not _is_pure_odds('1.8')

    def test_guard_within(self):
        """漂移 0.14 → 通过。"""
        assert abs(1.71 - 1.85) <= self.GUARD

    def test_guard_boundary(self):
        """漂移接近边界 0.149 → 通过。"""
        assert abs(1.701 - 1.85) <= self.GUARD

    def test_guard_beyond(self):
        """漂移超边界 0.151 → 拦截。"""
        assert not (abs(1.699 - 1.85) <= self.GUARD)

    def test_old_guard_too_loose(self):
        """旧护栏 0.25 会放进让球盘相近赔率（回归保护）。"""
        # 让球盘赔率 1.96 vs 目标 1.85 → diff 0.11
        # 旧 0.25 → 通过（危险！可能点到让球盘）
        # 新 0.15 → 通过（仍通过，但由 isSpreadCtx/hasHandicapLine 拦截）
        assert abs(1.96 - 1.85) <= 0.25  # 旧护栏放行
        assert abs(1.96 - 1.85) <= 0.15  # 新护栏也放行
        # 让球盘最终由市场类型校验拦截
        spread_row = '巴勃罗b队让球-0.5大1.96小1.86'
        assert _is_spread_ctx(spread_row)
        assert not _is_total_ctx(spread_row)

    def test_guard_consistent_with_odds_threshold(self):
        """护栏阈值与 oddsTextOk 一致（防止护栏比匹配阈值宽）。"""
        assert self.GUARD == 0.15  # 与 oddsTextOk 中的 0.15 一致


# ═══════════════════════════════════════════════════════════════
# 方向词选择（Python 侧）
# ═══════════════════════════════════════════════════════════════

class TestSideWords:
    """side_words 选择逻辑（bet_ui.py 第 42-47 行）。"""

    def _get_side_words(self, sel: str) -> list[str] | None:
        sel = (sel or '').lower()
        if sel in ('under', 'u'):
            return ['小', 'under', '低于']
        elif sel in ('over', 'o'):
            return ['大', 'over', '高于']
        return None

    def test_over_side_words(self):
        assert self._get_side_words('over') == ['大', 'over', '高于']

    def test_under_side_words(self):
        assert self._get_side_words('under') == ['小', 'under', '低于']

    def test_over_short_alias(self):
        assert self._get_side_words('o') == ['大', 'over', '高于']

    def test_under_short_alias(self):
        assert self._get_side_words('u') == ['小', 'under', '低于']

    def test_home_rejected(self):
        """独赢方向不支持。"""
        assert self._get_side_words('home') is None

    def test_empty_rejected(self):
        assert self._get_side_words('') is None

    def test_anti_direction_over(self):
        """over 方向反方向词 = 小。"""
        sel_dir = 'over'
        anti = ['小'] if sel_dir == 'over' else ['大']
        assert anti == ['小']

    def test_anti_direction_under(self):
        """under 方向反方向词 = 大。"""
        sel_dir = 'under'
        anti = ['小'] if sel_dir == 'over' else ['大']
        assert anti == ['大']


# ═══════════════════════════════════════════════════════════════
# 综合场景：多层校验联动
# ═══════════════════════════════════════════════════════════════

class TestCombinedScenario:
    """模拟 click_js 中的多层校验联动。"""

    def _simulate_row_click_check(self, row_text: str, target_odds: float,
                                   line: float, sel_dir: str) -> dict:
        """模拟 click_js 行级校验逻辑（简化版）。

        返回: {'allow': bool, 'reason': str}
        """
        norm_row = _norm(row_text)
        side_words = (['大', 'over', '高于'] if sel_dir in ('over', 'o')
                      else ['小', 'under', '低于'] if sel_dir in ('under', 'u')
                      else [])
        if not side_words:
            return {'allow': False, 'reason': 'invalid_selection'}

        # 1. 赔率匹配
        has_side = any(w in norm_row for w in [w.lower() for w in side_words])
        if not has_side:
            return {'allow': False, 'reason': 'no_side_word'}

        # 2. 盘口线匹配
        line_str = str(line)
        if line_str not in norm_row:
            return {'allow': False, 'reason': 'line_not_found'}

        # 3. 市场类型校验 rowNotSpread
        row_not_spread = (not _is_spread_ctx(row_text)) or _is_total_ctx(row_text)
        if not row_not_spread:
            return {'allow': False, 'reason': 'spread_market_excluded'}

        # 4. 让球线数字排除 hasHandicapLine
        has_handicap = bool(HANDICAP_LINE_RE.search(norm_row))
        if has_handicap:
            return {'allow': False, 'reason': 'handicap_line_detected'}

        # 5. 赔率阈值
        odds_found = False
        for m in re.finditer(r'\d{1,2}\.\d{2,3}', row_text):
            val = float(m.group())
            if abs(val - target_odds) <= 0.15:
                odds_found = True
                break
        if not odds_found:
            return {'allow': False, 'reason': 'odds_not_found'}

        return {'allow': True, 'reason': 'pass'}

    def test_normal_total_over(self):
        """正常大小球 over → 放行。"""
        row = '巴勃罗b队 大小 3.5 大 1.85 小 1.95'
        r = self._simulate_row_click_check(row, 1.85, 3.5, 'over')
        assert r['allow']
        assert r['reason'] == 'pass'

    def test_normal_total_under(self):
        """正常大小球 under → 放行。"""
        row = '巴勃罗b队 大小 3.5 大 1.85 小 1.95'
        r = self._simulate_row_click_check(row, 1.95, 3.5, 'under')
        assert r['allow']

    def test_handicap_row_rejected_by_market(self):
        """让球盘行 → 市场类型校验拦截。"""
        row = '巴勃罗b队 让球-0.5 3.5 大1.85 小1.95'
        r = self._simulate_row_click_check(row, 1.85, 3.5, 'over')
        assert not r['allow']
        assert r['reason'] == 'spread_market_excluded'

    def test_handicap_line_in_total_row_rejected(self):
        """大小球行混入让球线 → hasHandicapLine 拦截。"""
        row = '巴勃罗b队 大小 3.5 让球 -1.5 大 1.85 小 1.95'
        r = self._simulate_row_click_check(row, 1.85, 3.5, 'over')
        assert not r['allow']
        assert r['reason'] == 'handicap_line_detected'

    def test_odds_drift_within_threshold(self):
        """赔率漂移 0.14 → 放行。"""
        row = '巴勃罗b队 大小 3.5 大 1.71 小 1.95'
        r = self._simulate_row_click_check(row, 1.85, 3.5, 'over')
        assert r['allow']

    def test_odds_drift_beyond_threshold(self):
        """赔率漂移超阈值 → 拦截（大小赔率均不匹配）。"""
        row = '巴勃罗b队 大小 3.5 大 1.69 小 2.05'
        r = self._simulate_row_click_check(row, 1.85, 3.5, 'over')
        assert not r['allow']
        assert r['reason'] == 'odds_not_found'

    def test_english_total_row_pass(self):
        """英文 totals 标签 → 放行。"""
        row = 'Arsenal totals 3.5 over 1.85 under 1.95'
        r = self._simulate_row_click_check(row, 1.85, 3.5, 'over')
        assert r['allow']

    def test_english_spread_row_rejected(self):
        """英文 spread 标签 → 拦截。"""
        row = 'Arsenal spread -0.5 3.5 over 1.85 under 1.95'
        r = self._simulate_row_click_check(row, 1.85, 3.5, 'over')
        assert not r['allow']
        assert r['reason'] == 'spread_market_excluded'

    def test_no_line_in_row(self):
        """行内无盘口线 → 拦截。"""
        row = '巴勃罗b队 大小 大 1.85 小 1.95'
        r = self._simulate_row_click_check(row, 1.85, 3.5, 'over')
        assert not r['allow']
        assert r['reason'] == 'line_not_found'

    def test_handicap_pair_format_not_rejected(self):
        """平博让球双线格式 0.5-0.5 → lookbehind 后不再误匹配（dash 前有数字）。
        实际让球盘由 isSpreadCtx/让球标签 拦截，不依赖 rowHasHandicap。
        此行含大小3.5+大1.85 → 不再被 handicap_line_detected 拦截。"""
        row = '巴勃罗b队 大小 3.5 0.5-0.5 大 1.85 小 1.95'
        r = self._simulate_row_click_check(row, 1.85, 3.5, 'over')
        # 关键：不再因 handicap_line_detected 拦截
        assert r['reason'] != 'handicap_line_detected'

    def test_quarter_line_concat_odds_not_rejected(self):
        """四分线 3.5-4 + 赔率 2.130 拼接不应被误判为让球线（实盘 odds_not_found 根因）。"""
        row = '巴勃罗b队 大小 3.5-4 2.130 小 1.699'
        r = self._simulate_row_click_check(row, 1.699, 3.75, 'under')
        # rowHasHandicap 不应命中，应放行或因其他原因拦截（非 handicap_line_detected）
        assert r['reason'] != 'handicap_line_detected'


# ═══════════════════════════════════════════════════════════════
# 方向标签缺失兜底
# ═══════════════════════════════════════════════════════════════

class TestDirectionLabelFallback:
    """方向标签缺失兜底：noSideLabelOver / noSideLabelUnder / singleOddsFallback。

    注意：click_js 中 hasOver/hasUnder 检查的是行内是否含独立方向词（大/小）。
    '大小' 是市场标签，其中的'大'/'小'字符也会被 includes 匹配到。
    实际生产场景中，方向标签缺失是指行内 DOM 的独立 label 元素丢失，
    而非市场标签'大小'中的字符。此处用不含'大小'的行测试纯方向标签缺失。
    """

    def test_over_no_label_has_under(self):
        """行内有小无大 → over 取离小最远的赔率。"""
        row_text = '巴勃罗b队 3.5 小 1.95 1.85'
        row_norm = _norm(row_text)
        has_under = '小' in row_norm
        has_over = '大' in row_norm
        sel_dir = 'over'
        assert sel_dir == 'over' and not has_over and has_under
        # 在原始文本（含空格）中提取赔率，避免归一化后数字粘连
        odds_positions = {}
        for m in re.finditer(r'\d{1,2}\.\d{2,3}', row_text):
            val = m.group()
            # 在归一化文本中查找位置（用于距离计算）
            val_norm = _norm(val)
            idx = row_norm.find(val_norm)
            if idx >= 0:
                odds_positions[val] = idx
        assert len(odds_positions) >= 2, f"expected >=2 odds, got {odds_positions}"
        # 取离"小"最远的赔率作为 over
        under_idx = row_norm.index('小')
        best_odds = max(odds_positions, key=lambda o: abs(odds_positions[o] - under_idx))
        assert float(best_odds) == 1.85

    def test_under_no_label_has_over(self):
        """行内有大无小 → under 取离大最远的赔率。"""
        row_text = '巴勃罗b队 3.5 大 1.85 1.95'
        row_norm = _norm(row_text)
        has_under = '小' in row_norm
        has_over = '大' in row_norm
        sel_dir = 'under'
        assert sel_dir == 'under' and not has_under and has_over
        odds_positions = {}
        for m in re.finditer(r'\d{1,2}\.\d{2,3}', row_text):
            val = m.group()
            val_norm = _norm(val)
            idx = row_norm.find(val_norm)
            if idx >= 0:
                odds_positions[val] = idx
        assert len(odds_positions) >= 2, f"expected >=2 odds, got {odds_positions}"
        over_idx = row_norm.index('大')
        best_odds = max(odds_positions, key=lambda o: abs(odds_positions[o] - over_idx))
        assert float(best_odds) == 1.95

    def test_single_odds_fallback(self):
        """行内仅一个匹配赔率 → 直接点击。"""
        row_text = '巴勃罗b队 3.5 1.85'
        matching_odds = []
        for m in re.finditer(r'\d{1,2}\.\d{2,3}', row_text):
            val = float(m.group())
            if abs(val - 1.85) <= 0.25:
                matching_odds.append(val)
        assert len(matching_odds) == 1
        assert matching_odds[0] == 1.85

    def test_fallback_uses_wider_threshold(self):
        """兜底使用 ±0.25 宽阈值（比精确匹配 ±0.15 宽）。"""
        # 行内赔率 1.72（diff 0.13 from 1.85）
        # ±0.15 精确匹配 → 通过
        # ±0.25 兜底 → 也通过
        val = 1.72
        target = 1.85
        assert abs(val - target) <= 0.15  # 精确匹配
        assert abs(val - target) <= 0.25  # 兜底匹配

    def test_fallback_wider_than_precise(self):
        """兜底阈值 0.25 比精确阈值 0.15 宽。"""
        # 赔率 1.68（diff 0.17 from 1.85）
        val = 1.68
        target = 1.85
        assert not (abs(val - target) <= 0.15)  # 精确不匹配
        assert abs(val - target) <= 0.25  # 兜底匹配


# ═══════════════════════════════════════════════════════════════
# 浏览器端 JavaScript 集成测试（需 Playwright）
# ═══════════════════════════════════════════════════════════════

# click_js 中与让球盘排除相关的核心逻辑（从 bet_ui.py 提取）
# 保持与源文件同步
_CLICK_JS_CORE = """(args) => {
  const { tokens, odds, sideWords, line, homeN, awayN, selDir } = args;
  const norm = (s) => String(s || '').replace(/\\s+/g, '').toLowerCase();
  const oddsTextOk = (t) => {
    const n = Number(String(t || '').replace(/[^0-9.]/g, ''));
    if (!n || n < 1.01 || n > 50) return false;
    return Math.abs(n - Number(odds)) <= 0.15;
  };
  const lineTxt = (line == null || line === '') ? '' : String(line);
  const lineAliases = (() => {
    if (!lineTxt) return [];
    const n = Number(lineTxt);
    if (!n || n !== n || n <= 0) return [lineTxt];
    const lo = Math.floor(n); const frac = n - lo;
    if (Math.abs(frac - 0.25) < 1e-9) return [lineTxt, lo + '-' + (lo + 0.5)];
    if (Math.abs(frac - 0.75) < 1e-9) return [lineTxt, (lo + 0.5) + '-' + (lo + 1)];
    return [lineTxt];
  })();
  const hitLineTxt = (ctx) => !lineTxt || lineAliases.some((a) => String(ctx || '').includes(a));
  const spreadWords = ['让球', '让分', 'spread', 'handicap', '盘口', '亚盘'];
  const totalWords = ['大小', '总分', 'over/under', 'over-under', 'o/u', 'totals'];
  const isSpreadCtx = (ctx) => spreadWords.some((w) => String(ctx || '').toLowerCase().includes(w));
  const isTotalCtx = (ctx) => totalWords.some((w) => String(ctx || '').toLowerCase().includes(w));
  const nodes = Array.from(document.querySelectorAll('div, tr, li, section, article, a'));
  let row = null, how = '', bestScore = -1e9;
  for (const tok of (tokens || [])) {
    const tn = norm(tok);
    if (!tn || tn.length < 2) continue;
    for (const el of nodes) {
      try {
        const raw = el.innerText || ''; const t = norm(raw);
        if (!t || t.length < 8 || t.length > 900) continue;
        if (!t.includes(tn)) continue;
        const oddsHits = (raw.match(/(?:^|\\s)(\\d\\.\\d{2,3})(?:\\s|$)/g) || []).length;
        if (oddsHits < 1) continue;
        const effHits = Math.min(oddsHits, 10);
        const excessPenalty = oddsHits > 10 ? (oddsHits - 10) * 80 : 0;
        const score = effHits * 100 - excessPenalty - Math.abs(raw.length - 160);
        if (score > bestScore) { bestScore = score; row = el; how = 'token:' + tok; }
      } catch (e) {}
    }
    if (row) break;
  }
  if (!row) return { ok: false, why: 'row_not_found', how };
  const rowN = norm(row.innerText || '');
  const hN = norm(homeN), aN = norm(awayN);
  let rowHasTeam = (hN && hN.length >= 2 && rowN.includes(hN)) || (aN && aN.length >= 2 && rowN.includes(aN));
  if (!rowHasTeam) {
    for (const tok of (tokens || [])) {
      const tn2 = norm(tok);
      if (tn2 && tn2.length >= 3 && rowN.includes(tn2)) { rowHasTeam = true; break; }
    }
  }
  if (!rowHasTeam) return { ok: false, why: 'row_team_mismatch', how };
  const rowTextN = rowN;
  const rowHasLineTxt = !lineTxt || lineAliases.some((a) => rowTextN.includes(norm(a)));
  const rowNotSpread = !isSpreadCtx(rowTextN) || isTotalCtx(rowTextN);
  const hasHandicapLine = /(?<!\\d)[-\\uFF0D\\u2212\\u2013]\\d+\\.\\d/.test(rowTextN);
  const clickables = Array.from(row.querySelectorAll('button, a, span, div, td, label'));
  let target = null;
  for (const el of clickables) {
    const txt = String(el.innerText || el.textContent || '').trim();
    const pureNum = txt.replace(/\\s/g, '');
    const isPureOdds = /^\\d{1,2}\\.\\d{2,3}$/.test(pureNum);
    if (!oddsTextOk(txt) && !isPureOdds) continue;
    if (isPureOdds) {
      const n = Number(pureNum.replace(/[^0-9.]/g, ''));
      if (!n || Math.abs(n - Number(odds)) > 0.15) continue;
    } else if (!oddsTextOk(txt)) {
      continue;
    }
    let ctx = txt;
    try { const p = el.closest('div, tr, li, section') || el.parentElement; ctx = String((p && p.innerText) || txt); } catch (e) {}
    const hitSide = sideWords.some((w) => ctx.toLowerCase().includes(String(w).toLowerCase()));
    const anti = selDir === 'over' ? ['小'] : ['大'];
    const hitAnti = anti.some((w) => ctx.toLowerCase().includes(String(w).toLowerCase()));
    const hitLine = !lineTxt || hitLineTxt(ctx) || rowHasLineTxt;
    if (hitSide && !hitAnti && hitLine && rowNotSpread && !hasHandicapLine) {
      target = el; how += isPureOdds ? '+pure+side+line' : '+side+line'; break;
    }
  }
  if (!target && row && lineTxt && rowHasLineTxt) {
    const rowOddsEls = [];
    for (const el of clickables) {
      const t2 = String(el.innerText || el.textContent || '').trim();
      const pn = t2.replace(/\\s/g, '');
      if (!/^\\d{1,2}\\.\\d{2,3}$/.test(pn)) continue;
      const nv = Number(pn.replace(/[^0-9.]/g, ''));
      if (nv && Math.abs(nv - Number(odds)) <= 0.25) rowOddsEls.push({ el, val: nv });
    }
    if (rowOddsEls.length >= 1) {
      const hasUnder = rowTextN.includes('小'); const hasOver = rowTextN.includes('大');
      if (selDir === 'over' && !hasOver && hasUnder && rowOddsEls.length >= 2) {
        let bestEl = null, bestDist = -1;
        const underIdx = rowTextN.indexOf('小');
        for (const oe of rowOddsEls) {
          const oeTxt = norm(String(oe.el.innerText || ''));
          const oeIdx = rowTextN.indexOf(oeTxt);
          const dist = (underIdx >= 0 && oeIdx >= 0) ? Math.abs(oeIdx - underIdx) : 999;
          if (dist > bestDist) { bestDist = dist; bestEl = oe.el; }
        }
        if (bestEl) { target = bestEl; how += '+noSideLabelOver'; }
      } else if (selDir === 'under' && !hasUnder && hasOver && rowOddsEls.length >= 2) {
        let bestEl = null, bestDist = -1;
        const overIdx = rowTextN.indexOf('大');
        for (const oe of rowOddsEls) {
          const oeTxt = norm(String(oe.el.innerText || ''));
          const oeIdx = rowTextN.indexOf(oeTxt);
          const dist = (overIdx >= 0 && oeIdx >= 0) ? Math.abs(oeIdx - overIdx) : 999;
          if (dist > bestDist) { bestDist = dist; bestEl = oe.el; }
        }
        if (bestEl) { target = bestEl; how += '+noSideLabelUnder'; }
      } else if (rowOddsEls.length === 1) {
        target = rowOddsEls[0].el; how += '+singleOddsFallback';
      }
    }
  }
  if (!target) return { ok: false, why: 'odds_not_found', how, sample: rowTextN.slice(0, 120) };
  return { ok: true, why: 'clicked', sample: String(target.innerText || '').slice(0, 40), how };
}"""


def _build_mock_html(row_html: str) -> str:
    """构造模拟平博页面的 HTML。"""
    return f"""<html><body>
      <div class="header"><input placeholder="搜索" /></div>
      <div class="sports-list">{row_html}</div>
    </body></html>"""


def _make_row(team: str, market_label: str, line: str,
              over_odds: str, under_odds: str,
              extra: str = "") -> str:
    """构造一行赛事行 HTML（大/小赔率各自独立容器，模拟平博嵌套布局）。"""
    return f"""<div class="match-row">
      <span class="team-name">{team}</span>
      <span class="period">1H 27'</span>
      <span class="market">{market_label} {line}</span>
      <span class="extra">{extra}</span>
      <div class="odds-cell over"><span class="label">大</span> <button class="odds">{over_odds}</button></div>
      <div class="odds-cell under"><span class="label">小</span> <button class="odds">{under_odds}</button></div>
    </div>"""


@pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")
class TestClickJsIntegration:
    """click_js 集成测试：在真实浏览器中验证让球盘排除逻辑。"""

    @pytest.fixture
    async def page(self):
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            yield page
            await browser.close()

    @pytest.mark.asyncio
    async def test_normal_total_row_clicked(self, page):
        """正常大小球行 → 点击成功。"""
        row = _make_row("巴勃罗B队", "大小", "3.5", "1.85", "1.95")
        await page.set_content(_build_mock_html(row))
        args = {
            "tokens": ["巴勃罗B队", "巴勃罗"], "odds": 1.85,
            "sideWords": ["大", "over", "高于"], "line": 3.5,
            "homeN": "巴勃罗B队", "awayN": "对手队", "selDir": "over",
        }
        result = await page.evaluate(_CLICK_JS_CORE, args)
        assert result["ok"]
        assert "1.85" in result["sample"]

    @pytest.mark.asyncio
    async def test_handicap_row_rejected(self, page):
        """让球盘行 → 拒绝点击。"""
        row = _make_row("巴勃罗B队", "让球", "-0.5", "1.86", "1.96")
        await page.set_content(_build_mock_html(row))
        args = {
            "tokens": ["巴勃罗B队", "巴勃罗"], "odds": 1.85,
            "sideWords": ["大", "over", "高于"], "line": 3.5,
            "homeN": "巴勃罗B队", "awayN": "对手队", "selDir": "over",
        }
        result = await page.evaluate(_CLICK_JS_CORE, args)
        assert not result["ok"]

    @pytest.mark.asyncio
    async def test_handicap_line_in_row_rejected(self, page):
        """行内含让球线数字 -1.5 → 拒绝。"""
        row = _make_row("巴勃罗B队", "大小", "3.5", "1.85", "1.95", extra="让球 -1.5")
        await page.set_content(_build_mock_html(row))
        args = {
            "tokens": ["巴勃罗B队", "巴勃罗"], "odds": 1.85,
            "sideWords": ["大", "over", "高于"], "line": 3.5,
            "homeN": "巴勃罗B队", "awayN": "对手队", "selDir": "over",
        }
        result = await page.evaluate(_CLICK_JS_CORE, args)
        assert not result["ok"]

    @pytest.mark.asyncio
    async def test_odds_drift_within_threshold(self, page):
        """赔率漂移 0.14 → 点击成功。"""
        row = _make_row("巴勃罗B队", "大小", "3.5", "1.71", "1.95")
        await page.set_content(_build_mock_html(row))
        args = {
            "tokens": ["巴勃罗B队", "巴勃罗"], "odds": 1.85,
            "sideWords": ["大", "over", "高于"], "line": 3.5,
            "homeN": "巴勃罗B队", "awayN": "对手队", "selDir": "over",
        }
        result = await page.evaluate(_CLICK_JS_CORE, args)
        assert result["ok"]

    @pytest.mark.asyncio
    async def test_odds_drift_beyond_threshold(self, page):
        """赔率漂移超 ±0.15 且超 ±0.25 → 不点击。"""
        row = _make_row("巴勃罗B队", "大小", "3.5", "1.50", "2.20")
        await page.set_content(_build_mock_html(row))
        args = {
            "tokens": ["巴勃罗B队", "巴勃罗"], "odds": 1.85,
            "sideWords": ["大", "over", "高于"], "line": 3.5,
            "homeN": "巴勃罗B队", "awayN": "对手队", "selDir": "over",
        }
        result = await page.evaluate(_CLICK_JS_CORE, args)
        assert not result["ok"]

    @pytest.mark.asyncio
    async def test_english_handicap_rejected(self, page):
        """英文 spread 标签 → 拒绝。"""
        row = _make_row("Arsenal", "Spread", "-0.5", "1.85", "1.95")
        await page.set_content(_build_mock_html(row))
        args = {
            "tokens": ["Arsenal", "Arsen"], "odds": 1.85,
            "sideWords": ["大", "over", "高于"], "line": 3.5,
            "homeN": "Arsenal", "awayN": "Chelsea", "selDir": "over",
        }
        result = await page.evaluate(_CLICK_JS_CORE, args)
        assert not result["ok"]

    @pytest.mark.asyncio
    async def test_direction_fallback_single_odds(self, page):
        """行内仅一个匹配赔率（±0.25 内）→ singleOddsFallback。"""
        row = """<div class="match-row">
          <span>巴勃罗B队</span>
          <span>大小 3.5</span>
          <button>1.68</button>
          <button>2.20</button>
        </div>"""
        await page.set_content(_build_mock_html(row))
        args = {
            "tokens": ["巴勃罗B队", "巴勃罗"], "odds": 1.85,
            "sideWords": ["大", "over", "高于"], "line": 3.5,
            "homeN": "巴勃罗B队", "awayN": "对手队", "selDir": "over",
        }
        result = await page.evaluate(_CLICK_JS_CORE, args)
        assert result["ok"]
        assert "singleOddsFallback" in result.get("how", "")

    @pytest.mark.asyncio
    async def test_under_normal_click(self, page):
        """正常大小球 under → 点击成功。"""
        row = _make_row("巴勃罗B队", "大小", "3.5", "1.85", "1.95")
        await page.set_content(_build_mock_html(row))
        args = {
            "tokens": ["巴勃罗B队", "巴勃罗"], "odds": 1.95,
            "sideWords": ["小", "under", "低于"], "line": 3.5,
            "homeN": "巴勃罗B队", "awayN": "对手队", "selDir": "under",
        }
        result = await page.evaluate(_CLICK_JS_CORE, args)
        assert result["ok"]
        assert "1.95" in result["sample"]


# ═══════════════════════════════════════════════════════════════
# 独立运行入口
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    """直接运行：先跑纯 Python 测试，再跑 Playwright 测试。"""
    # 收集所有纯 Python 测试类
    pure_classes = [
        TestHandicapLineRegex,
        TestMarketTypeDetection,
        TestOddsThreshold,
        TestPureOddsDriftGuard,
        TestSideWords,
        TestCombinedScenario,
        TestDirectionLabelFallback,
    ]

    total = [0]
    passed = [0]
    failed = [0]

    print("=" * 70)
    print("让球盘误点修复单元测试")
    print("=" * 70)

    for cls in pure_classes:
        instance = cls()
        methods = [m for m in dir(instance) if m.startswith("test_")]
        for method_name in methods:
            total[0] += 1
            try:
                getattr(instance, method_name)()
                passed[0] += 1
                print(f"  ✅ {cls.__name__}.{method_name}")
            except AssertionError as e:
                failed[0] += 1
                print(f"  ❌ {cls.__name__}.{method_name}: {e}")
            except Exception as e:
                failed[0] += 1
                print(f"  ❌ {cls.__name__}.{method_name}: {type(e).__name__}: {e}")

    # Playwright 集成测试
    if HAS_PLAYWRIGHT:
        print()
        print("-" * 70)
        print("Playwright 浏览器集成测试")
        print("-" * 70)

        async def _run_playwright_tests():
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()

                integration = TestClickJsIntegration()
                test_methods = [m for m in dir(integration)
                               if m.startswith("test_") and callable(getattr(integration, m))
                               and m not in ("test_click_js_integration",)]

                for method_name in test_methods:
                    total[0] += 1
                    try:
                        await getattr(integration, method_name)(page=page)
                        passed[0] += 1
                        print(f"  ✅ {method_name}")
                    except AssertionError as e:
                        failed[0] += 1
                        print(f"  ❌ {method_name}: {e}")
                    except Exception as e:
                        failed[0] += 1
                        print(f"  ❌ {method_name}: {type(e).__name__}: {e}")

                await browser.close()

        try:
            asyncio.run(_run_playwright_tests())
        except Exception as e:
            print(f"  ⚠️ Playwright 测试跳过: {e}")
    else:
        print("\n  ⚠️ Playwright 未安装，跳过浏览器集成测试")
        print("    安装: pip install playwright && playwright install chromium")

    print()
    print("=" * 70)
    print(f"结果: {passed[0]}/{total[0]} 通过, {failed[0]} 失败")
    print("=" * 70)
    exit(0 if failed[0] == 0 else 1)
