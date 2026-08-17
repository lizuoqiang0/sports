import asyncio

from app.services.bookmakers.plugins.pinnacle.venue import (
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
