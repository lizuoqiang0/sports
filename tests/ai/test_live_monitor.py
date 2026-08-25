from __future__ import annotations

import asyncio
from unittest.mock import patch

from app.services.live_monitor import _check_ai_engine_status


class _FakeCache:
    def __init__(self):
        self.keys: list[str] = []

    async def get(self, key: str):
        self.keys.append(key)
        return "1" if key == "ai:engine:running:6" else "worker-token"


def test_engine_monitor_reads_requested_user_key_not_user_one():
    fake = _FakeCache()
    with patch("app.core.cache.cache", fake):
        issues, status = asyncio.run(_check_ai_engine_status(6))

    assert issues == []
    assert status["running"] is True
    assert fake.keys == ["ai:engine:running:6", "ai:engine:lock:6"]
