"""赔率落库：保证 (match_id, bet_type, provider, valid_from) 唯一；变更时版本化保留初盘。"""
from __future__ import annotations

import itertools
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

_seq = itertools.count(1)


def unique_valid_from(base: datetime | None = None) -> datetime:
    """生成进程内/跨 worker 几乎不冲突的 valid_from。"""
    n = next(_seq)
    if base is None:
        base = datetime.now(timezone.utc)
    if getattr(base, "tzinfo", None) is not None:
        base = base.astimezone(timezone.utc).replace(tzinfo=None)
    # pid + 单调序号，避免多 worker 同秒碰撞；timedelta 会自动进位秒
    pid_skew = (os.getpid() % 1000) * 1000
    return base + timedelta(microseconds=pid_skew + n)


def public_odds_data(odds_data: dict | None) -> dict:
    """去掉内部 _ob 引用，供比较/展示。"""
    if not isinstance(odds_data, dict):
        return {}
    return {k: v for k, v in odds_data.items() if not str(k).startswith("_")}


def _num_close(a: Any, b: Any, eps: float = 0.001) -> bool:
    try:
        if a is None and b is None:
            return True
        if a is None or b is None:
            return False
        return abs(float(a) - float(b)) <= eps
    except (TypeError, ValueError):
        return a == b


def odds_data_changed(old: dict | None, new: dict | None) -> bool:
    a = public_odds_data(old)
    b = public_odds_data(new)
    if set(a.keys()) != set(b.keys()):
        return True
    for k, v in a.items():
        if not _num_close(v, b.get(k)):
            return True
    return False


def odds_materially_changed(
    *,
    old_odds_data: dict | None,
    new_odds_data: dict | None,
    old_spread: Any = None,
    new_spread: Any = None,
    old_total: Any = None,
    new_total: Any = None,
) -> bool:
    """赔率或盘口线（让球/大小）有实质变化则 True。"""
    if odds_data_changed(old_odds_data, new_odds_data):
        return True
    if not _num_close(old_spread, new_spread, eps=0.01):
        return True
    if not _num_close(old_total, new_total, eps=0.01):
        return True
    return False


def apply_odds_version(
    db: AsyncSession,
    *,
    current: Any | None,
    match_id: int,
    bet_type: Any,
    odds_data: dict,
    spread: Optional[float],
    total: Optional[float],
    provider: str,
    is_live: bool,
    now: datetime,
    odds_cls: Any,
) -> tuple[Any, bool]:
    """有变化则关闭旧版本并插入新行；无变化则保持。返回 (当前有效行, 是否写入新版本)。"""
    if current is not None and not odds_materially_changed(
        old_odds_data=getattr(current, "odds_data", None),
        new_odds_data=odds_data,
        old_spread=getattr(current, "spread", None),
        new_spread=spread,
        old_total=getattr(current, "total", None),
        new_total=total,
    ):
        # 赔率未变时也要刷新站点内部定位元数据。平博 UI 下单依赖最新
        # mid/原生队名；只比较公开赔率会令历史行永久缺失这些信息。
        if isinstance(odds_data, dict) and dict(getattr(current, "odds_data", None) or {}) != odds_data:
            current.odds_data = dict(odds_data)
        return current, False

    close_at = now.replace(tzinfo=None) if getattr(now, "tzinfo", None) else now
    if current is not None:
        current.valid_to = close_at

    row = odds_cls(
        match_id=match_id,
        bet_type=bet_type,
        odds_data=odds_data,
        spread=spread,
        total=total,
        provider=provider,
        is_live=is_live,
        valid_from=unique_valid_from(now),
    )
    db.add(row)
    return row, True
