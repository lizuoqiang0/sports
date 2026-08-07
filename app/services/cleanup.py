"""
数据清理服务 - 定期删除超过保留期的投注记录和赛事记录。
"""
import asyncio
import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy import select, delete, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.user import Bet, Match, Odds, MatchContextRow, Transaction

logger = logging.getLogger(__name__)

_task: asyncio.Task | None = None


async def _cleanup_once() -> dict:
    """执行一次清理，返回删除统计。"""
    retention_hours = int(settings.DATA_RETENTION_HOURS)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=retention_hours)
    stats = {"bets": 0, "matches": 0, "odds": 0, "contexts": 0, "transactions_unlinked": 0}

    async with AsyncSessionLocal() as db:
        # 1. 删除过期投注记录（Transaction.bet_id 会被 SET NULL）
        result = await db.execute(
            delete(Bet).where(Bet.created_at < cutoff)
        )
        stats["bets"] = result.rowcount or 0

        # 2. 删除没有剩余投注的过期赛事（Odds / MatchContextRow 会 CASCADE 删除）
        #    先找出有剩余投注的赛事 ID，排除它们
        remaining_match_ids = await db.execute(
            select(Bet.match_id).where(Bet.match_id.isnot(None)).distinct()
        )
        protected_ids = {row[0] for row in remaining_match_ids}

        # 查询过期赛事
        old_matches = await db.execute(
            select(Match.id).where(Match.created_at < cutoff)
        )
        old_match_ids = [row[0] for row in old_matches]

        # 过滤掉有剩余投注的赛事
        deletable_match_ids = [mid for mid in old_match_ids if mid not in protected_ids]

        if deletable_match_ids:
            # 手动删除 Odds（虽然 CASCADE 也会处理，但显式删除更可控）
            await db.execute(
                delete(Odds).where(Odds.match_id.in_(deletable_match_ids))
            )
            # 手动删除 MatchContextRow
            await db.execute(
                delete(MatchContextRow).where(MatchContextRow.match_id.in_(deletable_match_ids))
            )
            # 删除赛事
            result = await db.execute(
                delete(Match).where(Match.id.in_(deletable_match_ids))
            )
            stats["matches"] = result.rowcount or 0

        await db.commit()

    if stats["bets"] or stats["matches"]:
        logger.info(
            "数据清理完成: 删除 %s 笔投注, %s 场赛事 (保留期 %sh)",
            stats["bets"], stats["matches"], retention_hours,
        )
    return stats


async def _cleanup_loop():
    """每小时执行一次清理。"""
    # 启动后等 60 秒再首次执行，避免与启动流程冲突
    await asyncio.sleep(60)
    while True:
        try:
            await _cleanup_once()
        except Exception as e:
            logger.warning("数据清理失败: %s", e)
        await asyncio.sleep(3600)


def start_cleanup_task():
    global _task
    if _task and not _task.done():
        return
    _task = asyncio.create_task(_cleanup_loop(), name="ob-cleanup")


def stop_cleanup_task():
    global _task
    if _task and not _task.done():
        _task.cancel()
    _task = None
