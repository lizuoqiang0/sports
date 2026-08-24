"""nowscore 当日全量赛事上下文预取：周期性爬取所有比赛数据并缓存到 Redis + DB。

启动后每 NOWSCORE_PREFETCH_INTERVAL_SEC 秒执行一次：
1. 足球：1 次 HTTP 获取 scheduleId 列表 -> 批量获取标题 -> 并发解析所有比赛
2. 篮球：同上

AI 分析时直接从 Redis 读取，无需实时爬取。
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from app.config import settings
from app.core.worker_leader import current_background_leader, is_background_leader, worker_id

logger = logging.getLogger(__name__)

_task: Optional[asyncio.Task] = None
_interval = int(getattr(settings, "NOWSCORE_PREFETCH_INTERVAL_SEC", 3600) or 3600)
_concurrency = int(getattr(settings, "NOWSCORE_PREFETCH_CONCURRENCY", 10) or 10)
_tick_sec = min(max(15, _interval // 12), 300)
_last_result_key = "nowscore:prefetch:last_result"


async def _is_enabled() -> bool:
    """运行时开关：Redis > .env 默认值。"""
    from app.core.cache import cache
    try:
        val = await cache.get("nowscore:prefetch:enabled")
        if val is not None:
            return str(val) in ("1", "true", "True", "on")
    except Exception:
        pass
    return bool(getattr(settings, "NOWSCORE_PREFETCH_ENABLED", False))


async def _get_last_result() -> dict | None:
    from app.core.cache import cache

    try:
        data = await cache.get_json(_last_result_key)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


async def _set_last_result(payload: dict) -> None:
    from app.core.cache import cache

    try:
        await cache.set_json(_last_result_key, payload, ttl=0)
    except Exception:
        pass


async def get_prefetch_status() -> dict:
    enabled = await _is_enabled()
    last_result = await _get_last_result()
    now = int(time.time())
    leader_owner = await current_background_leader()
    next_due_at = None
    if enabled:
        if isinstance(last_result, dict) and last_result.get("finished_at"):
            try:
                next_due_at = int(last_result["finished_at"]) + _interval
            except Exception:
                next_due_at = now + _interval
        else:
            next_due_at = now
    return {
        "enabled": enabled,
        "interval_sec": _interval,
        "concurrency": _concurrency,
        "leader": bool(leader_owner),
        "leader_owner": leader_owner,
        "process_is_leader": bool(is_background_leader()),
        "worker_id": worker_id(),
        "tick_sec": _tick_sec,
        "last_result": last_result,
        "next_due_at": next_due_at,
    }


async def _prefetch_loop() -> None:
    """主循环：预取当日全量赛事上下文。"""
    await asyncio.sleep(20)  # 启动后等待其他服务就绪

    while True:
        try:
            enabled = await _is_enabled()
            if not enabled or not is_background_leader():
                await asyncio.sleep(_tick_sec)
                continue

            last_result = await _get_last_result()
            now = int(time.time())
            last_finished = 0
            if isinstance(last_result, dict):
                try:
                    last_finished = int(last_result.get("finished_at") or 0)
                except Exception:
                    last_finished = 0
            if last_finished > 0 and now - last_finished < _interval:
                await asyncio.sleep(_tick_sec)
                continue

            from app.services.nowscore_scraper import prefetch_today_all_contexts
            started_at = int(time.time())

            # 足球
            football_count = await prefetch_today_all_contexts(
                sport="football",
                concurrency=_concurrency,
            )
            logger.info("nowscore prefetch: football cached=%d", football_count)

            # 篮球
            basketball_count = await prefetch_today_all_contexts(
                sport="basketball",
                concurrency=_concurrency,
            )
            logger.info("nowscore prefetch: basketball cached=%d", basketball_count)
            finished_at = int(time.time())
            result = {
                "started_at": started_at,
                "finished_at": finished_at,
                "football_cached": int(football_count),
                "basketball_cached": int(basketball_count),
                "ok": True,
                "error": None,
            }
            await _set_last_result(result)
            try:
                from app.core.websocket import manager
                await manager.broadcast_all({
                    "type": "ai_prefetch_done",
                    "data": {
                        "football": int(football_count),
                        "basketball": int(basketball_count),
                        "elapsed_sec": finished_at - started_at,
                        "source": "scheduled",
                        "ok": True,
                    },
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
            except Exception:
                pass

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("nowscore prefetch loop error: %s", e)
            finished_at = int(time.time())
            await _set_last_result(
                {
                    "started_at": finished_at,
                    "finished_at": finished_at,
                    "football_cached": 0,
                    "basketball_cached": 0,
                    "ok": False,
                    "error": str(e),
                }
            )
            try:
                from app.core.websocket import manager
                await manager.broadcast_all({
                    "type": "ai_prefetch_done",
                    "data": {
                        "football": 0,
                        "basketball": 0,
                        "elapsed_sec": 0,
                        "source": "scheduled",
                        "ok": False,
                        "error": str(e),
                    },
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
            except Exception:
                pass

        await asyncio.sleep(_tick_sec)


def start_nowscore_prefetcher() -> None:
    global _task
    if _task and not _task.done():
        return
    _task = asyncio.create_task(_prefetch_loop(), name="nowscore_prefetcher")
    logger.info(
        "nowscore prefetcher started (interval=%ss concurrency=%s tick=%ss)",
        _interval, _concurrency, _tick_sec,
    )


def stop_nowscore_prefetcher() -> None:
    global _task
    if _task and not _task.done():
        _task.cancel()
    _task = None
    logger.info("nowscore prefetcher stopped")
