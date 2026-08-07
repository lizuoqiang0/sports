"""
管理 API - 数据源开关 + 手动预取
"""
import asyncio
import logging
from fastapi import APIRouter, Depends

from app.config import settings
from app.core.security import get_current_user
from app.schemas import APIResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/admin", tags=["管理"])

_SWITCH_KEY = "nowscore:prefetch:enabled"


async def _get_switch() -> bool:
    """读取运行时开关（Redis > .env 默认值）。"""
    from app.core.cache import cache
    try:
        val = await cache.get(_SWITCH_KEY)
        if val is not None:
            return str(val) in ("1", "true", "True", "on")
    except Exception:
        pass
    return bool(getattr(settings, "NOWSCORE_PREFETCH_ENABLED", False))


# === 数据源开关 ===
@router.get("/nowscore/switch", response_model=APIResponse)
async def get_nowscore_switch(
    _: object = Depends(get_current_user),
):
    """获取数据源开关状态。"""
    enabled = await _get_switch()
    return APIResponse(
        message="数据源开关状态",
        data={
            "enabled": enabled,
            "interval_sec": int(getattr(settings, "NOWSCORE_PREFETCH_INTERVAL_SEC", 3600)),
            "concurrency": int(getattr(settings, "NOWSCORE_PREFETCH_CONCURRENCY", 10)),
        },
    )


@router.post("/nowscore/switch", response_model=APIResponse)
async def toggle_nowscore_switch(
    enabled: bool = True,
    _: object = Depends(get_current_user),
):
    """设置数据源开关。打开=自动预取，关闭=停止预取。"""
    from app.core.cache import cache
    await cache.set(_SWITCH_KEY, "1" if enabled else "0", ttl=0)
    return APIResponse(
        message=f"数据源已{'开启' if enabled else '关闭'}",
        data={"enabled": enabled},
    )


# === 手动触发预取 ===
@router.post("/nowscore/prefetch", response_model=APIResponse)
async def trigger_nowscore_prefetch(
    sport: str = "all",
    _: object = Depends(get_current_user),
):
    """手动触发 nowscore 当日全量赛事上下文预取（后台执行，立即返回）。"""
    from app.core.cache import cache
    from app.services.nowscore_scraper import prefetch_today_all_contexts

    # 清除旧的进度数据，避免新预取误读已完成的旧进度
    await cache.delete("nowscore:prefetch:progress")

    sports = ["football", "basketball"] if sport == "all" else [sport]

    async def _run():
        for s in sports:
            try:
                count = await prefetch_today_all_contexts(sport=s, concurrency=10)
                logger.info("prefetch triggered: sport=%s cached=%d", s, count)
            except Exception as e:
                logger.error("prefetch triggered error: sport=%s %s", s, e)

    asyncio.create_task(_run())
    return APIResponse(
        message="预取已触发，后台执行中",
        data={"sports": sports},
    )


# === 预取进度 ===
@router.get("/nowscore/progress", response_model=APIResponse)
async def get_prefetch_progress(
    _: object = Depends(get_current_user),
):
    """获取当前预取进度。"""
    from app.core.cache import cache
    try:
        data = await cache.get_json("nowscore:prefetch:progress")
        if data:
            return APIResponse(message="预取进行中", data=data)
    except Exception:
        pass
    return APIResponse(message="无进行中的预取任务", data=None)
