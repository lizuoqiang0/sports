"""平博页面状态恢复逻辑单元测试。

覆盖本轮修复的核心函数（纯逻辑部分，不依赖浏览器）：
- odds_change: 下单赔率变动策略（≥1.7 接受 / <1.7 放弃）
- venue: 白屏判定 JS 逻辑的 Python 侧边界 + 直达 URL 生成
- bet_ui: 站点拒绝清理的关键词匹配语义
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.services.bookmakers.odds_change import (  # noqa: E402
    decide_odds_change,
    odds_meaningfully_changed,
)
from app.services.bookmakers.plugins.pinnacle.venue import (  # noqa: E402
    page_shows_maintenance,
    pinnacle_page_is_blank,
    pinnacle_live_sport_urls,
    pinnacle_session_expired,
    recover_pinnacle_blank_page,
    recover_pinnacle_maintenance_page,
)
from app.services.bookmakers.site_profiles import needs_manual_venue  # noqa: E402


class TestOddsChangePolicy:
    """赔率变动决策：防滑点保护（floor=1.7）。"""

    def test_unchanged_accepts(self):
        ok, why, use = decide_odds_change(1.925, 1.925)
        assert ok and use == 1.925

    def test_drop_below_floor_rejects(self):
        # 实盘案例：波兹南 1.925 → 1.613，必须放弃
        ok, why, use = decide_odds_change(1.925, 1.613)
        assert not ok
        assert "放弃" in why

    def test_drop_but_above_floor_accepts(self):
        ok, why, use = decide_odds_change(1.925, 1.75)
        assert ok and use == 1.75
        assert "接受" in why

    def test_rise_accepts(self):
        ok, why, use = decide_odds_change(1.80, 2.10)
        assert ok and use == 2.10

    def test_exactly_floor_accepts(self):
        ok, _, use = decide_odds_change(1.90, 1.70)
        assert ok and use == 1.70

    def test_no_current_uses_original(self):
        ok, _, use = decide_odds_change(1.85, None)
        assert ok and use == 1.85

    def test_no_original_uses_current(self):
        ok, why, use = decide_odds_change(None, 1.95)
        assert ok and use == 1.95

    def test_both_missing_rejects(self):
        ok, _, _ = decide_odds_change(None, None)
        assert not ok

    def test_invalid_values_ignored(self):
        # 非法值（0、负数、超界）按 None 处理
        ok, _, _ = decide_odds_change(0, 1.85)
        assert ok

    def test_eps_threshold(self):
        # 微小变动（<0.005）视为未变
        assert not odds_meaningfully_changed(1.90, 1.902)
        assert odds_meaningfully_changed(1.90, 1.91)


class TestPinnacleVenueUrls:
    """滚球直达 URL 生成。"""

    def test_from_origin(self):
        urls = pinnacle_live_sport_urls(origin="https://www.rowilong.com")
        assert len(urls) == 4
        assert any("/soccer/live" in u for u in urls)
        assert any("/basketball/live" in u for u in urls)
        assert all(u.startswith("https://www.rowilong.com") for u in urls)

    def test_from_page_url(self):
        urls = pinnacle_live_sport_urls(
            page_url="https://www.rowilong.com/zh-cn/compact/sports/soccer/live"
        )
        assert urls and "/soccer/live" in urls[0]

    def test_no_input_empty(self):
        assert pinnacle_live_sport_urls() == []

    def test_pinnacle_compact_entry_is_automatic(self):
        assert needs_manual_venue("pinnacle") is False
        assert needs_manual_venue("ob") is True


class TestPinnacleAuthentication:
    class Page:
        url = "https://www.rowilong.com/zh-cn/compact/sports/soccer/live"

        def is_closed(self):
            return False

        async def evaluate(self, _script):
            return {"guest": True, "loginSurface": True}

    async def test_guest_sportsbook_is_expired(self):
        assert await pinnacle_session_expired(self.Page()) is True


class TestPinnaclePageRecovery:
    class BlankPage:
        url = "https://www.rowilong.com/zh-cn/compact/sports/soccer/live"

        def __init__(self):
            self.blank = True
            self.reloads = 0
            self.gotos: list[str] = []

        def is_closed(self):
            return False

        async def evaluate(self, script):
            if "PINNACLE_BLANK_STATE" in script:
                return {
                    "ready": "complete",
                    "textLength": 0 if self.blank else 80,
                    "hasVisibleControl": not self.blank,
                }
            raise AssertionError("unexpected script")

        async def reload(self, **_kwargs):
            self.reloads += 1
            # 模拟真实故障：reload 成功返回，但 SPA 仍是空 root。

        async def goto(self, url, **_kwargs):
            self.gotos.append(url)
            self.url = url
            self.blank = False

        async def wait_for_timeout(self, _milliseconds):
            return None

    async def test_blank_reload_success_but_still_blank_uses_goto(self):
        page = self.BlankPage()
        assert await pinnacle_page_is_blank(page) is True
        assert await recover_pinnacle_blank_page(page, attempts=1) is True
        assert page.reloads == 1
        assert page.gotos and "/compact/sports/" in page.gotos[0]
        assert await pinnacle_page_is_blank(page) is False

    class MaintenancePage:
        url = "https://www.rowilong.com/zh-cn/compact/sports/soccer/live"

        def __init__(self):
            self.maintenance = True
            self.reloads = 0

        def is_closed(self):
            return False

        async def evaluate(self, script):
            if "PINNACLE_MAINTENANCE_STATE" in script:
                return {"maintenance": self.maintenance, "text": "Pinnacle888正在维护中，暂时不可用"}
            if "PINNACLE_BLANK_STATE" in script:
                return {"ready": "complete", "textLength": 80, "hasVisibleControl": True}
            raise AssertionError("unexpected script")

        async def reload(self, **_kwargs):
            self.reloads += 1
            self.maintenance = False

        async def goto(self, url, **_kwargs):
            self.url = url
            self.maintenance = False

        async def wait_for_timeout(self, _milliseconds):
            return None

    async def test_maintenance_page_refreshes_until_normal(self):
        page = self.MaintenancePage()
        assert await page_shows_maintenance(page) is True
        assert await recover_pinnacle_maintenance_page(page, attempts=2) is True
        assert page.reloads == 1
        assert await page_shows_maintenance(page) is False


class TestRejectCleanupKeywords:
    """站点拒绝关键词匹配：决定是否触发投注单清理。"""

    REJECT_KWS = (
        "当前选项不适用", "余额不足", "不能低于", "已取消", "无法",
        "失败", "限额", "拒绝", "不能接受", "失效", "错误",
    )

    def _should_cleanup(self, msg: str) -> bool:
        return bool(msg) and any(k in msg for k in self.REJECT_KWS)

    def test_reject_messages_trigger_cleanup(self):
        for msg in (
            "当前选项不适用于投注",
            "余额不足，请先充值",
            "无法接受该投注",
            "投注失败，请稍后重试",
        ):
            assert self._should_cleanup(msg), msg

    def test_success_messages_do_not_cleanup(self):
        for msg in ("投注已提交", "已接受", "投注成功", ""):
            assert not self._should_cleanup(msg), msg
