import asyncio

from app.services.bookmakers.site_session import KeptSiteSession, SiteSessionManager


class FakePlaywright:
    def __init__(self):
        self.stopped = False

    async def stop(self):
        self.stopped = True


class FakeBrowser:
    def __init__(self, name):
        self.name = name


def _session(key, browser, playwright):
    return KeptSiteSession(
        key=key,
        base_url="https://example.test",
        token="token",
        playwright=playwright,
        browser=browser,
        context=None,
        page=None,
        site_code="ob",
    )


def test_stale_browser_disconnect_does_not_remove_replacement_session(monkeypatch):
    manager = SiteSessionManager()
    old_pw = FakePlaywright()
    new_pw = FakePlaywright()
    old = _session("https://example.test:443", FakeBrowser("old"), old_pw)
    replacement = _session("https://example.test:443", FakeBrowser("new"), new_pw)
    manager._sessions[replacement.key] = replacement

    notified = []

    async def notify(session):
        notified.append(session)

    monkeypatch.setattr(manager, "_notify_backend_disconnected", notify)
    asyncio.run(manager._on_browser_disconnected(old))

    assert manager._sessions[replacement.key] is replacement
    assert old_pw.stopped is False
    assert new_pw.stopped is False
    assert notified == []
