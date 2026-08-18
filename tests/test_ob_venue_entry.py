import asyncio

from app.services.bookmakers.venue_entry import (
    _is_venue_url,
    is_in_sportsbook,
    page_already_on_live_board,
    page_looks_like_sportsbook,
)


class Frame:
    def __init__(self, url):
        self.url = url


class Page:
    def __init__(self, url, frames=()):
        self.url = url
        self.main_frame = object()
        self.frames = [self.main_frame, *frames]

    def is_closed(self):
        return False

    async def inner_text(self, _selector, **_kwargs):
        return "足球 LIVE 125 篮球 LIVE 72 让球&大小"


def test_ob_entry_shell_is_not_a_venue_or_live_board():
    page = Page("https://portal.example/game/sport/ob?enName=YBTY")

    assert _is_venue_url(page.url) is False
    assert asyncio.run(page_looks_like_sportsbook(page)) is False
    assert asyncio.run(is_in_sportsbook(page)) is False
    assert asyncio.run(page_already_on_live_board(page)) is False


def test_ob_sports_lobby_is_not_a_venue_or_live_board():
    page = Page("https://portal.example/game/sport")

    assert asyncio.run(page_looks_like_sportsbook(page)) is False
    assert asyncio.run(is_in_sportsbook(page)) is False


def test_ob_entry_shell_accepts_real_h5_frame():
    page = Page(
        "https://portal.example/game/sport/ob?enName=YBTY",
        [Frame("https://h5.example/app-h5/?token=live-token")],
    )

    assert asyncio.run(page_looks_like_sportsbook(page)) is True
    assert asyncio.run(is_in_sportsbook(page)) is True


def test_zlshelves_home_is_not_a_betting_page():
    assert _is_venue_url("https://user-pc-new.zlshelves.com/#/home") is False
    assert _is_venue_url("https://user-pc-new.zlshelves.com/#/") is False
