"""每日盈亏追踪：以午夜 0 点(UTC)的总资产为基线，计算当日盈亏。"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

from app.core.cache import cache

logger = logging.getLogger(__name__)

_PNL_KEY = "pnl:baseline:{user_id}:{date}"


async def get_daily_pnl(user_id: int, current_total: float) -> dict:
    """获取每日盈亏。

    - 首次调用时（或午夜基线不存在），以当前总资产作为基线
    - 盈亏 = 当前总资产 - 基线
    - 每日 UTC 0 点基线自动过期（TTL=90000s≈25h）
    """
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    key = _PNL_KEY.format(user_id=user_id, date=today)

    baseline_str = await cache.get(key)
    if baseline_str is None:
        # 当天首次：以当前总资产作为基线
        await cache.set(key, str(current_total), ttl=90000)
        baseline = current_total
        logger.info("daily pnl baseline set: user=%s baseline=%.2f", user_id, baseline)
    else:
        baseline = float(baseline_str)

    pnl = round(current_total - baseline, 2)
    return {
        "total_assets": round(current_total, 2),
        "daily_pnl": pnl,
        "baseline": round(baseline, 2),
    }
