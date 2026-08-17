"""
数据清理服务 - 定期删除超过保留期的投注记录和赛事记录。
"""
import asyncio
import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy import exists, select, delete

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.user import Bet, Match, Odds, MatchContextRow

logger = logging.getLogger(__name__)

_task: asyncio.Task | None = None
_MATCH_DELETE_BATCH_SIZE = 500


async def _cleanup_once() -> dict:
    """执行一次清理，返回删除统计。

    注单保留期独立取 7 天（匹配胜率统计窗口 recent_betting_stats(days=7)），
    且仅删除已结算注单 —— 未结算单关联真实资金与同场防重投，绝不能按
    created_at 一刀切删除（否则统计样本被截断、可能对同场重复下单）。
    """
    retention_hours = int(settings.DATA_RETENTION_HOURS)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=retention_hours)
    bet_retention_hours = max(retention_hours, 168)  # 注单至少保留 7 天
    bet_cutoff = datetime.now(timezone.utc) - timedelta(hours=bet_retention_hours)
    stats = {"bets": 0, "matches": 0, "odds": 0, "contexts": 0, "transactions_unlinked": 0}

    async with AsyncSessionLocal() as db:
        # 1. 删除过期且已结算的投注记录（Transaction.bet_id 会被 SET NULL）
        result = await db.execute(
            delete(Bet).where(
                Bet.created_at < bet_cutoff,
                Bet.settled_at.is_not(None),
            )
        )
        stats["bets"] = result.rowcount or 0

        # 2. 数据库侧排除仍有关联注单的赛事；分页避免全表 ID 拉进 Python
        #    内存和生成超大的 IN 参数。Odds / MatchContextRow 显式删除便于统计。
        has_remaining_bet = exists(
            select(1).where(Bet.match_id == Match.id)
        )
        while True:
            candidate_rows = await db.execute(
                select(Match.id)
                .where(Match.created_at < cutoff, ~has_remaining_bet)
                .order_by(Match.id)
                .limit(_MATCH_DELETE_BATCH_SIZE)
            )
            deletable_match_ids = list(candidate_rows.scalars())
            if not deletable_match_ids:
                break

            odds_result = await db.execute(
                delete(Odds).where(Odds.match_id.in_(deletable_match_ids))
            )
            contexts_result = await db.execute(
                delete(MatchContextRow).where(MatchContextRow.match_id.in_(deletable_match_ids))
            )
            result = await db.execute(
                delete(Match).where(Match.id.in_(deletable_match_ids))
            )
            stats["odds"] += odds_result.rowcount or 0
            stats["contexts"] += contexts_result.rowcount or 0
            stats["matches"] += result.rowcount or 0

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
