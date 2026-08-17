import asyncio

from app.services.bookmakers.plugins.pinnacle.venue import (
    clear_pinnacle_maintenance,
    page_shows_maintenance,
    pinnacle_page_is_blank,
    recover_pinnacle_blank_page,
)


class FakePage:
    def __init__(self, states):
        self.states = list(states)
        self.index = 0
        self.reloads = 0
        self.url = "https://example.test/zh-cn/compact/sports/soccer/live"

    def is_closed(self):
        return False

    async def evaluate(self, _script):
        return self.states[self.index]

    async def reload(self, **_kwargs):
        self.reloads += 1
        self.index = min(self.index + 1, len(self.states) - 1)

    async def wait_for_timeout(self, _milliseconds):
        return None


def test_blank_page_is_detected():
    page = FakePage([{"ready": "complete", "textLength": 0, "hasVisibleControl": False}])
    assert asyncio.run(pinnacle_page_is_blank(page)) is True


def test_login_page_is_not_treated_as_blank():
    page = FakePage([{"ready": "complete", "textLength": 0, "hasVisibleControl": False}])
    page.url = "https://example.test/zh-cn/login"
    assert asyncio.run(pinnacle_page_is_blank(page)) is False


def test_blank_page_reloads_until_recovered():
    page = FakePage([
        {"ready": "complete", "textLength": 0, "hasVisibleControl": False},
        {"ready": "complete", "textLength": 64, "hasVisibleControl": True},
    ])
    assert asyncio.run(recover_pinnacle_blank_page(page)) is True
    assert page.reloads == 1


def test_blank_page_stops_after_bounded_retries():
    page = FakePage([{"ready": "complete", "textLength": 0, "hasVisibleControl": False}])
    assert asyncio.run(recover_pinnacle_blank_page(page, attempts=2)) is False
    assert page.reloads == 2


class MaintenancePage:
    def __init__(self, *, reload_fails=False):
        self.url = "https://example.test/zh-cn/compact/sports/soccer/live"
        self.text = "系统维护中"
        self.reloads = 0
        self.gotos = []
        self.reload_fails = reload_fails

    def is_closed(self):
        return False

    async def evaluate(self, _script):
        return self.text

    async def reload(self, **_kwargs):
        self.reloads += 1
        if self.reload_fails:
            raise RuntimeError("reload failed")
        self.text = "滚球盘口已恢复"

    async def goto(self, url, **_kwargs):
        self.gotos.append(url)
        self.url = url
        self.text = "滚球盘口已恢复"

    async def wait_for_timeout(self, _milliseconds):
        return None


def test_maintenance_banner_reloads_until_removed():
    page = MaintenancePage()
    assert asyncio.run(page_shows_maintenance(page)) is True
    assert asyncio.run(clear_pinnacle_maintenance(page)) is True
    assert page.reloads == 1
    assert asyncio.run(page_shows_maintenance(page)) is False


def test_maintenance_banner_falls_back_to_live_url_after_reload_failure():
    page = MaintenancePage(reload_fails=True)
    assert asyncio.run(clear_pinnacle_maintenance(page)) is True
    assert page.gotos
    assert page.gotos[0].endswith("/soccer/live")
