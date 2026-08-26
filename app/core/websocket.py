"""
WebSocket 连接管理器 - 用于赔率实时推送

多 uvicorn worker 时通过 Redis Pub/Sub 跨进程扇出，保证任一 worker 广播
能到达其它 worker 上的连接。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from fastapi import WebSocket

logger = logging.getLogger(__name__)

_WS_FANOUT_CHANNEL = "ob:ws:fanout"
_SEND_TIMEOUT_SEC = 3.0


class ConnectionManager:
    """
    WebSocket连接池管理
    - 支持多频道订阅 (赛事/赔率/通知)
    - 心跳保活
    - Redis 跨 worker 扇出
    - 自动清理断开连接
    """

    def __init__(self):
        self.active_connections: dict[int, set[WebSocket]] = {}
        self.subscriptions: dict[WebSocket, set[str]] = {}
        self.channel_subscribers: dict[str, set[WebSocket]] = {}
        self._lock = asyncio.Lock()
        self._fanout_task: Optional[asyncio.Task] = None
        self._origin = ""

    async def start_fanout(self) -> None:
        """订阅 Redis，接收其它 worker 的广播。"""
        if self._fanout_task and not self._fanout_task.done():
            return
        try:
            from app.core.worker_leader import worker_id

            self._origin = worker_id()
        except Exception:
            self._origin = f"pid-{id(self)}"
        self._fanout_task = asyncio.create_task(self._fanout_loop(), name="ob-ws-fanout")

    async def stop_fanout(self) -> None:
        if self._fanout_task and not self._fanout_task.done():
            self._fanout_task.cancel()
            try:
                await self._fanout_task
            except asyncio.CancelledError:
                pass
        self._fanout_task = None

    async def _fanout_loop(self) -> None:
        from app.core.cache import cache

        while True:
            pubsub = None
            try:
                pubsub = await cache.subscribe(_WS_FANOUT_CHANNEL)
                async for message in pubsub.listen():
                    if message is None:
                        continue
                    if message.get("type") != "message":
                        continue
                    raw = message.get("data")
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8", errors="ignore")
                    try:
                        payload = json.loads(raw)
                    except Exception:
                        continue
                    if payload.get("origin") == self._origin:
                        continue
                    await self._apply_fanout(payload)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("ws fanout listener error; retry in 2s")
                await asyncio.sleep(2)
            finally:
                if pubsub is not None:
                    try:
                        await pubsub.unsubscribe(_WS_FANOUT_CHANNEL)
                        await pubsub.close()
                    except Exception:
                        pass

    async def _publish_fanout(self, payload: dict) -> None:
        try:
            from app.core.cache import cache

            body = {**payload, "origin": self._origin or payload.get("origin") or ""}
            await cache.client.publish(_WS_FANOUT_CHANNEL, json.dumps(body, ensure_ascii=False))
        except Exception:
            # Redis 不可用时仍保证本进程内推送
            pass

    async def _apply_fanout(self, payload: dict) -> None:
        kind = payload.get("kind")
        message = payload.get("message") or {}
        if kind == "channel":
            await self._local_broadcast_channel(str(payload.get("channel") or ""), message)
        elif kind == "user":
            try:
                uid = int(payload.get("user_id"))
            except Exception:
                return
            await self._local_broadcast_user(uid, message)
        elif kind == "all":
            await self._local_broadcast_all(message)

    async def connect(
        self,
        websocket: WebSocket,
        user_id: int,
        subprotocol: Optional[str] = None,
    ):
        await websocket.accept(subprotocol=subprotocol)
        async with self._lock:
            if user_id not in self.active_connections:
                self.active_connections[user_id] = set()
            self.active_connections[user_id].add(websocket)
            self.subscriptions[websocket] = set()
        logger.info("WS connected: user=%s, total=%s", user_id, self._total_connections())

    async def disconnect(self, websocket: WebSocket, user_id: int):
        async with self._lock:
            if user_id in self.active_connections:
                self.active_connections[user_id].discard(websocket)
                if not self.active_connections[user_id]:
                    del self.active_connections[user_id]
            channels = self.subscriptions.pop(websocket, set())
            for channel in channels:
                self.channel_subscribers.get(channel, set()).discard(websocket)
        logger.info("WS disconnected: user=%s", user_id)

    async def subscribe(self, websocket: WebSocket, channel: str):
        async with self._lock:
            self.subscriptions.setdefault(websocket, set()).add(channel)
            self.channel_subscribers.setdefault(channel, set()).add(websocket)
        await self._send(websocket, {"type": "subscribed", "channel": channel})

    async def unsubscribe(self, websocket: WebSocket, channel: str):
        async with self._lock:
            self.subscriptions.get(websocket, set()).discard(channel)
            self.channel_subscribers.get(channel, set()).discard(websocket)
        await self._send(websocket, {"type": "unsubscribed", "channel": channel})

    async def _local_broadcast_channel(self, channel: str, message: dict) -> None:
        subscribers = tuple(self.channel_subscribers.get(channel, set()))
        if not subscribers:
            return
        delivered = await asyncio.gather(
            *(self._send(ws, message) for ws in subscribers),
            return_exceptions=True,
        )
        disconnected = [
            ws for ws, ok in zip(subscribers, delivered)
            if ok is not True
        ]
        if disconnected:
            await self._drop_connections(disconnected)

    async def _local_broadcast_user(self, user_id: int, message: dict) -> None:
        connections = tuple(self.active_connections.get(user_id, set()))
        if connections:
            delivered = await asyncio.gather(
                *(self._send(ws, message) for ws in connections),
                return_exceptions=True,
            )
            disconnected = [
                ws for ws, ok in zip(connections, delivered) if ok is not True
            ]
            if disconnected:
                await self._drop_connections(disconnected)

    async def _local_broadcast_all(self, message: dict) -> None:
        all_ws = set()
        for conns in self.active_connections.values():
            all_ws.update(conns)
        connections = tuple(all_ws)
        if connections:
            delivered = await asyncio.gather(
                *(self._send(ws, message) for ws in connections),
                return_exceptions=True,
            )
            disconnected = [
                ws for ws, ok in zip(connections, delivered) if ok is not True
            ]
            if disconnected:
                await self._drop_connections(disconnected)

    async def _drop_connections(self, websockets: list[WebSocket]) -> None:
        """移除已经超时或断开的连接，避免其反复拖慢赔率广播。"""
        stale = set(websockets)
        async with self._lock:
            for ws in stale:
                for channel in self.subscriptions.pop(ws, set()):
                    self.channel_subscribers.get(channel, set()).discard(ws)
            for user_id, connections in list(self.active_connections.items()):
                connections.difference_update(stale)
                if not connections:
                    del self.active_connections[user_id]

    async def broadcast_to_channel(self, channel: str, message: dict):
        await self._local_broadcast_channel(channel, message)
        await self._publish_fanout({"kind": "channel", "channel": channel, "message": message})

    async def broadcast_to_user(self, user_id: int, message: dict):
        await self._local_broadcast_user(user_id, message)
        await self._publish_fanout({"kind": "user", "user_id": user_id, "message": message})

    async def broadcast_all(self, message: dict):
        await self._local_broadcast_all(message)
        await self._publish_fanout({"kind": "all", "message": message})

    async def _send(self, ws: WebSocket, message: dict) -> bool:
        try:
            await asyncio.wait_for(ws.send_json(message), timeout=_SEND_TIMEOUT_SEC)
            return True
        except Exception:
            return False

    def _total_connections(self) -> int:
        return sum(len(v) for v in self.active_connections.values())

    async def heartbeat_loop(self):
        while True:
            await asyncio.sleep(30)
            ping_msg = json.dumps({"type": "ping", "timestamp": asyncio.get_event_loop().time()})
            all_ws = set()
            for conns in self.active_connections.values():
                all_ws.update(conns)
            connections = tuple(all_ws)
            if not connections:
                continue
            delivered = await asyncio.gather(
                *(self._send_text(ws, ping_msg) for ws in connections),
                return_exceptions=True,
            )
            disconnected = [
                ws for ws, ok in zip(connections, delivered) if ok is not True
            ]
            if disconnected:
                await self._drop_connections(disconnected)

    async def _send_text(self, ws: WebSocket, message: str) -> bool:
        try:
            await asyncio.wait_for(ws.send_text(message), timeout=_SEND_TIMEOUT_SEC)
            return True
        except Exception:
            return False


manager = ConnectionManager()


class WSEventType:
    ODDS_UPDATE = "odds_update"
    MATCH_STATUS = "match_status"
    BET_PLACED = "bet_placed"
    SUBSCRIBED = "subscribed"
    UNSUBSCRIBED = "unsubscribed"
