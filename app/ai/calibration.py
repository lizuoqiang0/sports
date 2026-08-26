"""最终置信度校准：仅在足量生产结算样本上修正模型概率。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select

from app.core.cache import cache
from app.database import AsyncSessionLocal
from app.models.user import Bet, BetStatus, Match, MatchStatus

_CACHE_TTL = 300
_CACHE_KEY = "ai:calibration:v3"
_BUCKET_WIDTH = 0.10
_MIN_SAMPLES = 12


def _bucket(confidence: float) -> str:
    lower = int(float(confidence) / _BUCKET_WIDTH) * _BUCKET_WIDTH
    return f"{lower:.2f}-{lower + _BUCKET_WIDTH:.2f}"


async def load_calibration_table(user_id: Optional[int] = None) -> dict[str, dict]:
    """读取30天已结算AI注单；少于12笔的分桶不参与概率修正。"""
    key = f"{_CACHE_KEY}:{user_id}" if user_id is not None else _CACHE_KEY
    try:
        cached = await cache.get_json(key)
        if isinstance(cached, dict) and cached.get("_loaded"):
            return cached
    except Exception:
        pass

    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=30)
    conditions = [
        Bet.settled_at.is_not(None),
        Bet.created_at >= since,
        Bet.is_ai_bet.is_(True),
        Bet.status == BetStatus.SUCCESS,
        Match.status == MatchStatus.FINISHED,
    ]
    if user_id is not None:
        conditions.append(Bet.user_id == int(user_id))
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(Bet).join(Match, Bet.match_id == Match.id).where(*conditions)
        )).scalars().all()

    result: dict[str, dict] = {"under": {}, "over": {}}
    decided = 0
    for bet in rows:
        selection = str(bet.selection or "").lower()
        if selection not in result or bet.ai_confidence is None:
            continue
        stake = float(bet.stake or 0)
        payout = float(bet.actual_payout or 0)
        if abs(payout - stake) <= 1e-9:
            continue
        key_name = _bucket(float(bet.ai_confidence))
        item = result[selection].setdefault(key_name, {"settled": 0, "won": 0})
        item["settled"] += 1
        item["won"] += int(payout > stake)
        decided += 1
    for selection in ("under", "over"):
        for item in result[selection].values():
            item["win_rate"] = round(item["won"] / item["settled"], 4)
    result.update({"_loaded": True, "_total_settled": decided})
    try:
        await cache.set_json(key, result, ttl=_CACHE_TTL)
    except Exception:
        pass
    return result


def calibrate_confidence(
    raw_confidence: float,
    selection: str,
    calibration_table: dict,
) -> tuple[float, str]:
    """足量分桶才校准；小样本保持原概率，避免过度反应。"""
    raw = max(0.0, min(0.99, float(raw_confidence or 0)))
    item = (calibration_table.get(str(selection or "").lower()) or {}).get(_bucket(raw))
    if not item or int(item.get("settled") or 0) < _MIN_SAMPLES:
        return raw, "样本不足，未校准"
    observed = item.get("win_rate")
    if not isinstance(observed, (int, float)):
        return raw, "样本无有效胜率，未校准"
    # 50%收缩到模型原值，并限制单次变化±0.10，避免一个分桶支配决策。
    target = 0.5 * raw + 0.5 * float(observed)
    calibrated = raw + max(-0.10, min(0.10, target - raw))
    calibrated = round(max(0.0, min(0.99, calibrated)), 4)
    return calibrated, (
        f"收缩校准: {raw:.2f}→{calibrated:.2f} "
        f"(分桶实际胜率{float(observed):.1%}, n={int(item['settled'])})"
    )
