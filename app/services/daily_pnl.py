"""站点余额快照与日盈亏追踪。"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.core.cache import cache

logger = logging.getLogger(__name__)

_PNL_KEY = "pnl:baseline:{user_id}:{date}"
_BALANCE_SNAPSHOT_KEY = "balance:snapshot:{user_id}"


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


async def get_daily_pnl(user_id: int, current_total: float) -> dict:
    """获取日风控盈亏。

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


async def sync_balance_snapshot(user_id: int, current_total: float) -> dict:
    """同步站点余额快照，返回按网站余额增减计算的盈亏摘要。

    语义：
    - reference_balance: 首次成功采集到的网站总余额，作为长期对照基线
    - previous_balance: 上一份已记录的网站总余额
    - balance_delta: 当前总余额相对 reference_balance 的增减
    - balance_change: 当前总余额相对 previous_balance 的单次变动
    """
    total_assets = round(float(current_total or 0), 2)
    key = _BALANCE_SNAPSHOT_KEY.format(user_id=user_id)
    now_iso = datetime.now(timezone.utc).isoformat()
    snapshot = await cache.get_json(key)
    if not isinstance(snapshot, dict):
        snapshot = {}

    reference_balance = _to_float(snapshot.get("reference_balance"), total_assets)
    previous_balance = _to_float(snapshot.get("previous_balance"), total_assets)
    initialized = bool(snapshot)

    if not initialized and total_assets <= 0:
        return {
            "total_assets": total_assets,
            "balance_delta": 0.0,
            "balance_change": 0.0,
            "reference_balance": 0.0,
            "previous_balance": 0.0,
            "snapshot_updated_at": None,
            "pnl_mode": "site_balance_delta",
        }

    if not initialized:
        snapshot = {
            "reference_balance": total_assets,
            "previous_balance": total_assets,
            "snapshot_updated_at": now_iso,
        }
        await cache.set_json(key, snapshot, ttl=0)
        logger.info(
            "site balance snapshot initialized: user=%s reference=%.2f",
            user_id,
            total_assets,
        )
        reference_balance = total_assets
        previous_balance = total_assets
    elif abs(total_assets - previous_balance) >= 0.01:
        snapshot["previous_balance"] = total_assets
        snapshot["snapshot_updated_at"] = now_iso
        await cache.set_json(key, snapshot, ttl=0)
    else:
        now_iso = str(snapshot.get("snapshot_updated_at") or now_iso)

    return {
        "total_assets": total_assets,
        "balance_delta": round(total_assets - reference_balance, 2),
        "balance_change": round(total_assets - previous_balance, 2),
        "reference_balance": round(reference_balance, 2),
        "previous_balance": round(previous_balance, 2),
        "snapshot_updated_at": now_iso,
        "pnl_mode": "site_balance_delta",
    }
