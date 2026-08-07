"""nowscore 当日全量赛事上下文预取：周期性爬取所有比赛数据并缓存到 Redis + DB。

启动后每 NOWSCORE_PREFETCH_INTERVAL_SEC 秒执行一次：
1. 足球：1 次 HTTP 获取 scheduleId 列表 -> 批量获取标题 -> 并发解析所有比赛
2. 篮球：同上

AI 分析时直接从 Redis 读取，无需实时爬取。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from app.config import settings
from app.core.worker_leader import is_background_leader

logger = logging.getLogger(__name__)

_task: Optional[asyncio.Task] = None
_interval = int(getattr(settings, "NOWSCORE_PREFETCH_INTERVAL_SEC", 3600) or 3600)
_concurrency = int(getattr(settings, "NOWSCORE_PREFETCH_CONCURRENCY", 10) or 10)


async def _is_enabled() -> bool:
    """运行时开关：Redis > .env 默认值。"""
    from app.core.cache import cache
    try:
        val = await cache.get("nowscore:prefetch:enabled")
        if val is not None:
            return str(val) in ("1", "true", "True", "on")
    except Exception:
        pass
    return bool(getattr(settings, "NOWSCORE_PREFETCH_ENABLED", True))


async def _prefetch_loop() -> None:
    """主循环：预取当日全量赛事上下文。"""
    await asyncio.sleep(20)  # 启动后等待其他服务就绪

    while True:
        try:
            if not await _is_enabled():
                await asyncio.sleep(_interval)
                continue

            if not is_background_leader():
                await asyncio.sleep(_interval)
                continue

            from app.services.nowscore_scraper import prefetch_today_all_contexts

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

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("nowscore prefetch loop error: %s", e)

        await asyncio.sleep(_interval)


def start_nowscore_prefetcher() -> None:
    global _task
    if _task and not _task.done():
        return
    _task = asyncio.create_task(_prefetch_loop(), name="nowscore_prefetcher")
    logger.info(
        "nowscore prefetcher started (interval=%ss concurrency=%s)",
        _interval, _concurrency,
    )


def stop_nowscore_prefetcher() -> None:
    global _task
    if _task and not _task.done():
        _task.cancel()
    _task = None
    logger.info("nowscore prefetcher stopped")
