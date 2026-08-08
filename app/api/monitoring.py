"""监控辅助 API：投注模式等。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.security import get_current_user
from app.database import get_db
from app.models.user import Match, User
from app.schemas import APIResponse

router = APIRouter(tags=["监控"])


class BetModeBody(BaseModel):
    bet_mode: str = Field(..., description="manual=人工 | active=自动")


@router.get("/api/v1/monitoring/overview", response_model=APIResponse)
async def monitoring_overview(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from app.services.bet_mode import get_user_bet_mode, mode_flags
    from app.services.live_mode import force_live_mode

    match_count = (await db.execute(select(func.count()).select_from(Match))).scalar() or 0
    flags = mode_flags(get_user_bet_mode(user))
    return APIResponse(
        data={
            "local_match_count": match_count,
            "monitoring_enabled": getattr(settings, "MONITORING_ENABLED", True),
            "force_live_mode": force_live_mode(),
            **flags,
        }
    )


@router.get("/api/v1/monitoring/bet-mode", response_model=APIResponse)
async def get_bet_mode(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from app.services.bet_mode import get_user_bet_mode, mode_flags

    fresh = await db.get(User, user.id)
    return APIResponse(data=mode_flags(get_user_bet_mode(fresh or user)))


@router.put("/api/v1/monitoring/bet-mode", response_model=APIResponse)
async def put_bet_mode(
    body: BetModeBody,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from app.services.bet_mode import mode_flags, set_user_bet_mode

    fresh = await db.get(User, user.id)
    if not fresh:
        raise HTTPException(status_code=404, detail="用户不存在")
    mode = await set_user_bet_mode(db, fresh, body.bet_mode)
    await db.commit()
    return APIResponse(data=mode_flags(mode), message=f"已切换为{mode_flags(mode)['label']}模式")
