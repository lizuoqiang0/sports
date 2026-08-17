import asyncio

from app.core.websocket import ConnectionManager


class _SlowSocket:
    async def send_json(self, _message):
        await asyncio.sleep(1)


def test_slow_channel_subscriber_is_timed_out_and_removed(monkeypatch) -> None:
    from app.core import websocket

    monkeypatch.setattr(websocket, "_SEND_TIMEOUT_SEC", 0.01)
    manager = ConnectionManager()
    ws = _SlowSocket()
    manager.active_connections[7] = {ws}
    manager.subscriptions[ws] = {"odds:live"}
    manager.channel_subscribers["odds:live"] = {ws}

    asyncio.run(manager._local_broadcast_channel("odds:live", {"type": "update"}))

    assert ws not in manager.active_connections.get(7, set())
    assert ws not in manager.subscriptions
    assert ws not in manager.channel_subscribers["odds:live"]
