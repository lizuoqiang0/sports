"""
多 worker 协调：仅一个进程跑后台轮询（live/balance），避免重复刮数。
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import uuid
from typing import Optional

logger = logging.getLogger(__name__)

_LEADER_KEY = "ob:worker:leader:background"
_LEADER_TTL = 25
_REFRESH_SEC = 10

_worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
_is_leader = False
_task: Optional[asyncio.Task] = None


def worker_id() -> str:
    return _worker_id


def is_background_leader() -> bool:
    return _is_leader


async def current_background_leader() -> str | None:
    from app.core.cache import cache

    try:
        client = cache.client
    except RuntimeError:
        return None
    try:
        cur = await client.get(_LEADER_KEY)
    except Exception:
        return None
    if cur is None:
        return None
    if isinstance(cur, bytes):
        try:
            return cur.decode()
        except Exception:
            return None
    return str(cur)


async def _try_acquire() -> bool:
    from app.core.cache import cache

    try:
        client = cache.client
    except RuntimeError:
        return False
    # SET key id NX EX ttl
    ok = await client.set(_LEADER_KEY, _worker_id, nx=True, ex=_LEADER_TTL)
    if ok:
        return True
    cur = await client.get(_LEADER_KEY)
    if cur == _worker_id:
        # GET 与 EXPIRE 之间 key 可能过期并被其它 worker 抢到；必须原子校验
        # 所有权后再续租，否则旧 leader 会短暂和新 leader 同时执行后台任务。
        return await cache.extend_lock_if_owned(
            _LEADER_KEY, _worker_id, ttl_sec=_LEADER_TTL
        )
    return False


async def _leader_loop() -> None:
    global _is_leader
    while True:
        try:
            held = await _try_acquire()
            if held and not _is_leader:
                _is_leader = True
                logger.info("acquired background leader id=%s", _worker_id)
            elif not held and _is_leader:
                _is_leader = False
                logger.warning("lost background leader id=%s", _worker_id)
        except Exception:
            logger.exception("leader election tick failed")
        await asyncio.sleep(_REFRESH_SEC)


def start_leader_election() -> None:
    global _task
    if _task and not _task.done():
        return
    _task = asyncio.create_task(_leader_loop(), name="ob-worker-leader")


def stop_leader_election() -> None:
    global _task, _is_leader
    if _task and not _task.done():
        _task.cancel()
    _task = None
    _is_leader = False
