"""
后台余额轮询：刷新 OB / 平博 网站真实余额。
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

_BALANCE_INTERVAL_SEC = 30
_task: asyncio.Task | None = None
_lock = asyncio.Lock()


async def _list_user_ids() -> list[int]:
    from sqlalchemy import or_, select

    from app.database import AsyncSessionLocal
    from app.models.user import BookmakerAccount, BookmakerStatus

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(BookmakerAccount.user_id)
            .where(
                or_(
                    BookmakerAccount.status == BookmakerStatus.CONNECTED,
                    BookmakerAccount.session_token_encrypted.isnot(None),
                    BookmakerAccount.session_token_encrypted != "",
                )
            )
            .distinct()
        )
        user_ids = [row[0] for row in result.all() if row[0]]
        if not user_ids:
            result2 = await db.execute(select(BookmakerAccount.user_id).distinct())
            user_ids = [row[0] for row in result2.all() if row[0]]
        return user_ids


async def _refresh_user(uid: int) -> None:
    from app.database import AsyncSessionLocal
    from app.services.balances import load_site_balances
    from app.services.daily_pnl import sync_balance_snapshot

    async with AsyncSessionLocal() as db:
        try:
            sites = await load_site_balances(db, uid)
            total_assets = sum(float(site.get("balance") or 0) for site in sites)
            await sync_balance_snapshot(uid, total_assets)
            await db.commit()
            logger.info(
                "balance poller user=%s sites=%s",
                uid,
                {
                    s.get("code"): {
                        "bal": s.get("balance"),
                        "live": s.get("live"),
                        "src": s.get("source"),
                    }
                    for s in sites
                },
            )
        except Exception:
            logger.exception("balance poller user=%s failed", uid)
            await db.rollback()


async def _tick_once() -> None:
    if _lock.locked():
        return
    async with _lock:
        # Gate 正忙于盘口同步时跳过本轮，避免与 odds-sync 抢页面导致假余额
        try:
            from app.services.bookmakers.gate_client import is_gate_too_busy

            if await is_gate_too_busy(min_busy=2, timeout=2.0):
                logger.info("balance poller skip: gate busy")
                return
        except Exception:
            pass
        user_ids = await _list_user_ids()
        for uid in user_ids:
            await _refresh_user(uid)


async def _loop() -> None:
    await asyncio.sleep(8)
    while True:
        try:
            await _tick_once()
        except Exception:
            logger.exception("balance poller tick failed")
        await asyncio.sleep(_BALANCE_INTERVAL_SEC)


def start_balance_poller() -> None:
    global _task
    if _task and not _task.done():
        return
    _task = asyncio.create_task(_loop(), name="ob-balance-poller")
    logger.info("balance poller started (every %ss)", _BALANCE_INTERVAL_SEC)


def stop_balance_poller() -> None:
    global _task
    if _task and not _task.done():
        _task.cancel()
    _task = None
