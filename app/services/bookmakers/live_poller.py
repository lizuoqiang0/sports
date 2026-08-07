"""
后台滚球轮询：周期性从真站拉取 LIVE 比分/时钟/赔率并推送 WebSocket。

策略：OB 与平博并行同步（Gate 已分站点车道），缩短双站刷新周期。
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

_LIVE_INTERVAL_SEC = 25
_task: asyncio.Task | None = None
_lock = asyncio.Lock()
_paused = False
_pause_depth = 0
_tick_n = 0


def pause_live_poller() -> None:
    """支持并发验证：多次 pause 需对应次数 resume 后才恢复轮询。"""
    global _paused, _pause_depth
    _pause_depth += 1
    _paused = True


def resume_live_poller() -> None:
    global _paused, _pause_depth
    _pause_depth = max(0, _pause_depth - 1)
    _paused = _pause_depth > 0


async def _list_connected_account_ids() -> list[int]:
    from sqlalchemy import and_, select

    from app.database import AsyncSessionLocal
    from app.models.user import BookmakerAccount, BookmakerStatus
    from app.services.bookmakers.registry import is_real_live_account
    from app.services.bookmakers.site_profiles import is_site_disabled

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(BookmakerAccount).where(
                and_(
                    BookmakerAccount.enabled.is_(True),
                    BookmakerAccount.status == BookmakerStatus.CONNECTED,
                    BookmakerAccount.code.in_(["ob", "pinnacle"]),
                )
            )
        )
        accounts = [
            a
            for a in result.scalars().all()
            if is_real_live_account(a.code, a.base_url or "")
            and not is_site_disabled(a.code)
        ]
        # 每站只留一个账号，OB 优先再平博（被 DISABLE 的站跳过）
        by_code: dict[str, int] = {}
        for a in accounts:
            code = str(a.code or "").lower()
            if code not in by_code:
                by_code[code] = int(a.id)
        ordered = []
        for code in ("ob", "pinnacle"):
            if code in by_code:
                ordered.append(by_code[code])
        return ordered


async def _gate_too_busy() -> bool:
    """Gate 已有 ≥2 条 lane 在忙时跳过本轮，给手动同步/登录留带宽。"""
    from app.services.bookmakers.gate_client import is_gate_too_busy

    return await is_gate_too_busy(min_busy=2, timeout=3.0)


async def _sync_one_account(acc_id: int, *, refresh_balance: bool) -> dict:
    from app.database import AsyncSessionLocal
    from app.services.bookmakers.sync import sync_live_scores_odds

    async with AsyncSessionLocal() as db:
        return await sync_live_scores_odds(
            db,
            user_id=None,
            only_account_id=acc_id,
            refresh_balance=refresh_balance,
        )


async def _tick_once() -> None:
    global _tick_n

    if _paused:
        logger.debug("live poller skip: paused for verify/login")
        return
    if _lock.locked():
        logger.debug("live poller skip: previous tick still running")
        return
    async with _lock:
        if await _gate_too_busy():
            logger.info("live poller skip: gate busy")
            return
        ids = await _list_connected_account_ids()
        if not ids:
            logger.debug("live poller skip: no connected accounts")
            return

        _tick_n += 1
        # 每 3 轮刷一次余额，减少与盘口抢车道
        refresh_balance = (_tick_n % 3) == 1

        async def _one(acc_id: int):
            if _paused:
                return {"skipped": True, "account_id": acc_id}
            try:
                result = await _sync_one_account(
                    acc_id, refresh_balance=refresh_balance
                )
                logger.info(
                    "live poller: account_id=%s matches=%s odds_writes=%s bal=%s",
                    acc_id,
                    result.get("matches"),
                    result.get("updated"),
                    refresh_balance,
                )
                return result
            except Exception:
                logger.exception("live poller session failed account_id=%s", acc_id)
                return {"error": True, "account_id": acc_id}

        # OB / 平博并行（各自独立浏览器车道）
        await asyncio.gather(*[_one(i) for i in ids])

        # 低频清理：独立短会话，不阻塞 Gate 抓取热路径
        try:
            from app.services.bookmakers.purge import maybe_run_periodic_purge

            await maybe_run_periodic_purge()
        except Exception:
            logger.exception("live poller purge failed")


async def _loop() -> None:
    # 启动稍等，让 DB/Redis 就绪
    await asyncio.sleep(6)
    while True:
        try:
            await _tick_once()
        except Exception:
            logger.exception("live poller tick failed")
        await asyncio.sleep(_LIVE_INTERVAL_SEC)


def start_live_poller() -> None:
    global _task
    if _task and not _task.done():
        return
    _task = asyncio.create_task(_loop(), name="ob-live-poller")
    logger.info(
        "live poller started (every %ss, parallel OB+平博)",
        _LIVE_INTERVAL_SEC,
    )


def stop_live_poller() -> None:
    global _task
    if _task and not _task.done():
        _task.cancel()
    _task = None
