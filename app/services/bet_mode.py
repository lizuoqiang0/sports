"""下单模式：人工(manual) / 自动(active)。

所有自动下单路径（机会扫描、AI 引擎等）必须读取本开关：
- 人工：只分析/生成机会，需用户确认后真实下单
- 自动：扫描通过后自动真实下单（体育站 place_bet）
"""
from __future__ import annotations

from typing import Literal, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.user import User

BetMode = Literal["manual", "active"]

MODE_MANUAL: BetMode = "manual"
MODE_ACTIVE: BetMode = "active"


def normalize_bet_mode(value: Optional[str]) -> BetMode:
    v = (value or "").strip().lower()
    if v in ("active", "auto", "自动", "主动"):
        return MODE_ACTIVE
    return MODE_MANUAL


def default_bet_mode() -> BetMode:
    """无用户偏好时：跟随 DEFAULT_BET_MODE。"""
    raw = getattr(settings, "DEFAULT_BET_MODE", None)
    if raw:
        return normalize_bet_mode(str(raw))
    return MODE_MANUAL


def get_user_bet_mode(user: Optional[User]) -> BetMode:
    if user is None:
        return default_bet_mode()
    raw = getattr(user, "bet_mode", None)
    if raw:
        return normalize_bet_mode(str(raw))
    return default_bet_mode()


def is_active_mode(user: Optional[User]) -> bool:
    return get_user_bet_mode(user) == MODE_ACTIVE


async def set_user_bet_mode(db: AsyncSession, user: User, mode: str) -> BetMode:
    normalized = normalize_bet_mode(mode)
    user.bet_mode = normalized
    await db.flush()
    return normalized


def mode_flags(mode: BetMode) -> dict:
    active = mode == MODE_ACTIVE
    return {
        "bet_mode": mode,
        "label": "自动" if active else "人工",
        "auto_confirm": active,
        "auto_execute": active,
        "description": (
            "自动模式：扫描通过风控后直接调用体育站真实下单"
            if active
            else "人工模式：仅生成机会/推荐，需手动确认后真实下单"
        ),
    }
