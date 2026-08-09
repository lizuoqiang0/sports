"""
管理 API - 数据源开关 + 手动预取
"""
import asyncio
import logging
from fastapi import APIRouter, Body, Depends, Query

from app.config import settings
from app.core.security import get_current_user
from app.schemas import APIResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/admin", tags=["管理"])

_SWITCH_KEY = "nowscore:prefetch:enabled"


def _actor_label(user: object) -> str:
    return str(
        getattr(user, "username", None)
        or getattr(user, "email", None)
        or getattr(user, "id", "")
    ).strip()


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


async def _get_prefetch_status() -> dict:
    from app.services.nowscore_prefetcher import get_prefetch_status

    try:
        return await get_prefetch_status()
    except Exception:
        enabled = await _get_switch()
        return {
            "enabled": enabled,
            "interval_sec": int(getattr(settings, "NOWSCORE_PREFETCH_INTERVAL_SEC", 3600)),
            "concurrency": int(getattr(settings, "NOWSCORE_PREFETCH_CONCURRENCY", 10)),
            "leader": False,
            "tick_sec": None,
            "last_result": None,
            "next_due_at": None,
        }


# === 数据源开关 ===
@router.get("/nowscore/switch", response_model=APIResponse)
async def get_nowscore_switch(
    _: object = Depends(get_current_user),
):
    """获取数据源开关状态。"""
    status = await _get_prefetch_status()
    return APIResponse(
        message="数据源开关状态",
        data=status,
    )


@router.post("/nowscore/switch", response_model=APIResponse)
async def toggle_nowscore_switch(
    enabled: bool = True,
    _: object = Depends(get_current_user),
):
    """设置数据源开关。打开=自动预取，关闭=停止预取。"""
    from app.core.cache import cache
    await cache.set(_SWITCH_KEY, "1" if enabled else "0", ttl=0)
    status = await _get_prefetch_status()
    return APIResponse(
        message=f"数据源已{'开启' if enabled else '关闭'}",
        data={**status, "enabled": enabled},
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
        started_at = int(__import__("time").time())
        counts = {"football": 0, "basketball": 0}
        ok = True
        error_msg = None
        for s in sports:
            try:
                count = await prefetch_today_all_contexts(sport=s, concurrency=10)
                counts[s] = int(count or 0)
                logger.info("prefetch triggered: sport=%s cached=%d", s, count)
            except Exception as e:
                ok = False
                error_msg = str(e)
                logger.error("prefetch triggered error: sport=%s %s", s, e)
        try:
            finished_at = int(__import__("time").time())
            await cache.set_json(
                "nowscore:prefetch:last_result",
                {
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "football_cached": int(counts.get("football") or 0),
                    "basketball_cached": int(counts.get("basketball") or 0),
                    "ok": ok,
                    "error": error_msg,
                },
                ttl=0,
            )
        except Exception:
            pass
        try:
            from app.core.websocket import manager
            await manager.broadcast_all({
                "type": "ai_prefetch_done",
                "data": {
                    "football": int(counts.get("football") or 0),
                    "basketball": int(counts.get("basketball") or 0),
                    "elapsed_sec": finished_at - started_at,
                    "source": "manual",
                    "ok": ok,
                    "error": error_msg,
                },
                "timestamp": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
            })
        except Exception:
            pass

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
    status = await _get_prefetch_status()
    return APIResponse(message="无进行中的预取任务", data={"running": False, **status})


@router.get("/nowscore/alias-candidates", response_model=APIResponse)
async def get_nowscore_alias_candidates(
    sport: str = Query("all", description="all / football / basketball"),
    limit: int = Query(100, ge=1, le=500),
    min_score: float = Query(0.0, ge=0.0, le=1.0),
    _: object = Depends(get_current_user),
):
    """查看未命中自动沉淀的候选别名清单。"""
    from app.services.nowscore_scraper import list_alias_candidates

    items = await list_alias_candidates(sport=sport, limit=limit, min_score=min_score)
    return APIResponse(
        message="候选别名清单",
        data={
            "sport": sport,
            "limit": limit,
            "min_score": min_score,
            "count": len(items),
            "items": items,
        },
    )


@router.post("/nowscore/alias-candidates/approve", response_model=APIResponse)
async def approve_nowscore_alias_candidate(
    candidate_id: str = Query(..., description="候选记录 ID"),
    delete_candidate: bool = Query(True, description="批准后是否从候选清单移除"),
    current_user: object = Depends(get_current_user),
):
    """一键把候选别名转为正式别名，立即参与匹配。"""
    from app.services.nowscore_scraper import approve_alias_candidate

    approved_by = _actor_label(current_user)
    item = await approve_alias_candidate(
        candidate_id,
        approved_by=approved_by,
        delete_candidate=delete_candidate,
    )
    if not item:
        return APIResponse(
            success=False,
            message="候选别名不存在或批准失败",
            error_code="ALIAS_CANDIDATE_NOT_FOUND",
        )
    return APIResponse(
        message="候选别名已转为正式别名",
        data=item,
    )


@router.post("/nowscore/alias-candidates/approve-batch", response_model=APIResponse)
async def approve_nowscore_alias_candidates_batch(
    payload: dict = Body(...),
    current_user: object = Depends(get_current_user),
):
    """批量批准候选别名，立即参与匹配。"""
    from app.services.nowscore_scraper import approve_alias_candidates

    candidate_ids = list(payload.get("candidate_ids") or [])
    delete_candidate = bool(payload.get("delete_candidate", True))
    result = await approve_alias_candidates(
        candidate_ids,
        approved_by=_actor_label(current_user),
        delete_candidate=delete_candidate,
    )
    return APIResponse(
        message="候选别名批量批准完成",
        data=result,
    )


@router.delete("/nowscore/alias-candidates", response_model=APIResponse)
async def clear_nowscore_alias_candidates(
    _: object = Depends(get_current_user),
):
    """清空候选别名清单。"""
    from app.services.nowscore_scraper import clear_alias_candidates

    cleared = await clear_alias_candidates()
    return APIResponse(
        message="候选别名清单已清空",
        data={"cleared": cleared},
    )


@router.get("/nowscore/alias-overrides", response_model=APIResponse)
async def get_nowscore_alias_overrides(
    sport: str = Query("all", description="all / football / basketball"),
    limit: int = Query(100, ge=1, le=500),
    _: object = Depends(get_current_user),
):
    """查看已经生效的正式别名。"""
    from app.services.nowscore_scraper import list_alias_overrides

    items = await list_alias_overrides(sport=sport, limit=limit)
    return APIResponse(
        message="正式别名清单",
        data={
            "sport": sport,
            "limit": limit,
            "count": len(items),
            "items": items,
        },
    )


@router.get("/nowscore/alias-overrides/export", response_model=APIResponse)
async def export_nowscore_alias_overrides(
    sport: str = Query("all", description="all / football / basketball"),
    limit: int = Query(5000, ge=1, le=5000),
    _: object = Depends(get_current_user),
):
    """导出正式别名。"""
    from app.services.nowscore_scraper import export_alias_overrides

    data = await export_alias_overrides(sport=sport, limit=limit)
    return APIResponse(
        message="正式别名导出成功",
        data=data,
    )


@router.post("/nowscore/alias-overrides/import", response_model=APIResponse)
async def import_nowscore_alias_overrides(
    payload: dict = Body(...),
    current_user: object = Depends(get_current_user),
):
    """导入正式别名。"""
    from app.services.nowscore_scraper import import_alias_overrides

    items = list(payload.get("items") or [])
    result = await import_alias_overrides(
        items,
        approved_by=_actor_label(current_user),
    )
    return APIResponse(
        message="正式别名导入完成",
        data=result,
    )


@router.post("/nowscore/alias-overrides/import-preview", response_model=APIResponse)
async def preview_import_nowscore_alias_overrides(
    payload: dict = Body(...),
    current_user: object = Depends(get_current_user),
):
    """预览正式别名导入差异。"""
    from app.services.nowscore_scraper import preview_alias_overrides_import

    items = list(payload.get("items") or [])
    result = await preview_alias_overrides_import(
        items,
        approved_by=_actor_label(current_user),
    )
    return APIResponse(
        message="正式别名导入预览完成",
        data=result,
    )


@router.get("/nowscore/alias-audit-logs", response_model=APIResponse)
async def get_nowscore_alias_audit_logs(
    limit: int = Query(100, ge=1, le=500),
    _: object = Depends(get_current_user),
):
    """查看别名管理操作日志。"""
    from app.services.nowscore_scraper import list_alias_audit_logs

    items = await list_alias_audit_logs(limit=limit)
    return APIResponse(
        message="别名操作日志",
        data={
            "limit": limit,
            "count": len(items),
            "items": items,
        },
    )


@router.delete("/nowscore/alias-overrides", response_model=APIResponse)
async def delete_nowscore_alias_overrides(
    record_id: str | None = Query(None, description="正式别名记录 ID；不传则清空全部"),
    payload: dict | None = Body(None),
    current_user: object = Depends(get_current_user),
):
    """删除一条、批量删除或清空全部正式别名。"""
    from app.services.nowscore_scraper import (
        clear_alias_overrides,
        delete_alias_override,
        delete_alias_overrides,
    )

    actor = _actor_label(current_user)
    record_ids = list((payload or {}).get("record_ids") or [])

    if record_ids:
        deleted = await delete_alias_overrides(record_ids, actor=actor)
        return APIResponse(
            message="正式别名批量删除完成",
            data=deleted,
        )

    if record_id:
        deleted = await delete_alias_override(record_id, actor=actor)
        return APIResponse(
            message="正式别名已删除",
            data={"deleted": deleted, "record_id": record_id},
        )

    cleared = await clear_alias_overrides(actor=actor)
    return APIResponse(
        message="正式别名清单已清空",
        data={"cleared": cleared},
    )
