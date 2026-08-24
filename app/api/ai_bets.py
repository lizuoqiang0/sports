"""
AI投注 API - 配置 / 启停 / 推荐 / 状态 / 一键投注
"""
import logging
from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.config import settings
from app.models.user import User, AIConfig, Match
from app.core.security import get_current_user
from app.ai.auto_better import (
    analyze_and_recommend,
    start_user_engine, stop_user_engine, get_engine_status
)
from app.ai.strategy import (
    ai_config_response_payload,
    effective_strategy_from_ai_config,
)
from app.schemas import APIResponse, AIConfigRequest

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/ai", tags=["AI投注"])


def compose_ai_runtime_status(
    *,
    status_info: dict | None,
    user: User,
    manual_analysis_sports: list[str] | None = None,
) -> dict:
    """统一 AI 引擎状态与下单模式，避免前端自行拼语义。"""
    from app.services.bet_mode import get_user_bet_mode, mode_flags

    flags = mode_flags(get_user_bet_mode(user))
    ai_enabled = bool(getattr(user, "ai_enabled", False))
    engine_running = bool((status_info or {}).get("running"))
    is_auto_mode = flags["bet_mode"] == "active"
    analysis_sports = list(manual_analysis_sports or [])
    manual_analysis_running = bool(analysis_sports)
    sports_label = " / ".join("足球" if s == "football" else "篮球" for s in analysis_sports) or "当前球类"

    if ai_enabled and engine_running and is_auto_mode:
        effective_state = "auto_running"
        effective_label = "自动运行中"
        effective_description = "系统正在分析并自动下单。"
        badge_tone = "green"
    elif manual_analysis_running:
        effective_state = "manual_analysis_running"
        effective_label = "手动分析中"
        effective_description = f"系统正在分析{sports_label}，只出推荐，不会自动下单。"
        badge_tone = "amber"
    elif ai_enabled and engine_running:
        effective_state = "manual_running"
        effective_label = "分析运行中"
        effective_description = "系统正在分析，只出推荐。"
        badge_tone = "amber"
    elif ai_enabled:
        effective_state = "enabled_stopped"
        effective_label = "AI 已开启"
        effective_description = "AI 已开启，但当前未运行。"
        badge_tone = "orange"
    elif is_auto_mode:
        effective_state = "mode_armed"
        effective_label = "自动模式未启动"
        effective_description = "已切到自动，但 AI 还没运行。"
        badge_tone = "slate"
    else:
        effective_state = "disabled"
        effective_label = "AI 未启动"
        effective_description = "当前不会分析，也不会下单。"
        badge_tone = "slate"

    scan_interval_sec = max(120, int(getattr(settings, "AI_SCAN_INTERVAL_SEC", 120) or 120))
    return {
        **(status_info or {}),
        **flags,
        "ai_enabled": ai_enabled,
        "engine_enabled": ai_enabled,
        "engine_running": engine_running,
        "manual_analysis_running": manual_analysis_running,
        "manual_analysis_sports": analysis_sports,
        "execution_mode": flags["bet_mode"],
        "execution_mode_label": flags["label"],
        "can_generate_recommendations": bool(manual_analysis_running or (ai_enabled and engine_running)),
        "can_auto_execute": bool(ai_enabled and engine_running and is_auto_mode),
        "effective_state": effective_state,
        "effective_label": effective_label,
        "effective_description": effective_description,
        "badge_tone": badge_tone,
        "runtime_limits": {
            "scan_interval_sec": scan_interval_sec,
            "scan_interval_min": round(scan_interval_sec / 60, 2),
            "stream_bet_mode": True,
        },
    }


# === AI配置 ===
@router.get("/config", response_model=APIResponse)
async def get_ai_config(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """获取AI投注配置"""
    result = await db.execute(select(AIConfig).where(AIConfig.user_id == current_user.id))
    config = result.scalar_one_or_none()

    return APIResponse(data=ai_config_response_payload(config))


@router.put("/config", response_model=APIResponse)
async def update_ai_config(
    req: AIConfigRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """更新AI投注配置"""
    result = await db.execute(select(AIConfig).where(AIConfig.user_id == current_user.id))
    config = result.scalar_one_or_none()

    if not config:
        config = AIConfig(user_id=current_user.id)
        db.add(config)

    from types import SimpleNamespace

    raw_snap = SimpleNamespace(
        max_bet_amount=req.max_bet_amount,
        max_daily_bets=req.max_daily_bets,
        min_confidence=req.min_confidence,
        preferred_sports=req.preferred_sports,
        excluded_teams=req.excluded_teams,
        stop_loss=req.stop_loss,
        take_profit=req.take_profit,
        max_odds=req.max_odds,
        min_odds=req.min_odds,
        use_llm_analysis=req.use_llm_analysis,
        is_active=bool(req.is_active),
    )
    effective = effective_strategy_from_ai_config(raw_snap)

    # 更新字段：落库即保存“生效值”，避免 UI 展示值与运行时生效值不一致
    config.strategy = effective.name
    config.max_bet_amount = effective.max_bet_amount
    config.max_daily_bets = effective.max_daily_bets
    config.min_confidence = effective.min_confidence
    config.is_active = bool(req.is_active)
    config.preferred_sports = req.preferred_sports
    config.excluded_teams = req.excluded_teams
    config.stop_loss = req.stop_loss
    config.take_profit = req.take_profit
    config.max_odds = effective.max_odds
    config.min_odds = effective.min_odds
    config.use_llm_analysis = effective.use_llm_analysis

    if not config.is_active:
        current_user.ai_enabled = False
    await db.flush()
    response_payload = ai_config_response_payload(config)
    await db.commit()
    if not config.is_active:
        await stop_user_engine(current_user.id)

    # 配置热更新：清推荐缓存 + 通知引擎/前端立即按新阈值生效
    try:
        from app.ai.recs_job import recs_cache_key
        from app.core.cache import cache
        from app.core.websocket import manager

        uid = int(current_user.id)
        for sport in ("football", "basketball"):
            for prov in ("", "ob", "pinnacle"):
                try:
                    await cache.delete(recs_cache_key(uid, sport, prov))
                except Exception:
                    pass
            try:
                await cache.delete(f"ai:recs:{uid}:{sport}")
            except Exception:
                pass
        # 版本戳：引擎循环可感知配置变更
        try:
            await cache.set(f"ai:strategy:ver:{uid}", str(int(__import__("time").time())), ttl=86400)
        except Exception:
            pass
        await manager.broadcast_to_user(
            uid,
            {
                "type": "ai_config_updated",
                "data": {
                    **response_payload,
                },
            },
        )
    except Exception as e:
        logger.warning("AI配置热更新副作用失败: %s", e)

    return APIResponse(
        message="AI 设置已保存",
        data=response_payload,
    )


# === 启停控制 ===
@router.post("/start", response_model=APIResponse)
async def start_ai(
    enabled: bool = True,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """启动/停止AI自动投注。是否自动真实下单由用户「人工/自动」开关决定。"""
    from app.ai.recs_job import list_analysis_watching_sports, stop_all_analysis_watches
    from app.services.bet_mode import get_user_bet_mode

    fresh_user = await db.get(User, current_user.id)
    if get_user_bet_mode(fresh_user or current_user) != "active":
        raise HTTPException(status_code=400, detail="请先切换到自动模式，再启动自动下单")

    current_user.ai_enabled = enabled
    await db.flush()

    if enabled:
        manual_analysis_sports = await list_analysis_watching_sports(current_user.id)
        if manual_analysis_sports:
            await stop_all_analysis_watches(user_id=current_user.id)
        # 确保有可用 AIConfig，避免引擎启动后立刻因无配置退出
        cfg_res = await db.execute(select(AIConfig).where(AIConfig.user_id == current_user.id))
        cfg = cfg_res.scalar_one_or_none()
        if not cfg:
            cfg = AIConfig(
                user_id=current_user.id,
                is_active=True,
                strategy="simple",
                preferred_sports=["football", "basketball"],
            )
            db.add(cfg)
        else:
            if not cfg.is_active:
                raise HTTPException(status_code=400, detail="请先在 AI 设置中开启 AI")
            if not bool(getattr(cfg, "use_llm_analysis", True)):
                raise HTTPException(status_code=400, detail="请先在 AI 设置中开启 AI分析，再启动自动下单")
            if not cfg.preferred_sports:
                cfg.preferred_sports = ["football", "basketball"]
        await db.flush()

        # ── 启动前预检风控：避免引擎启动后第一轮因风控自停 ──
        # 引擎 _run_cycle 会调用 _check_risk，如果风控不通过会自动关闭 ai_enabled
        # 并通过 WebSocket 通知前端关闭开关，导致"启动后立即自动关闭"现象。
        # 这里提前检查，不通过则直接返回 400，让前端开关不开启。
        from app.ai.strategy_gates import check_daily_risk
        from app.models.user import BookmakerAccount, BookmakerStatus
        from app.services.bookmakers.registry import is_real_live_account

        strat = effective_strategy_from_ai_config(cfg)
        triggered, why = await check_daily_risk(db, current_user.id, strat)
        if triggered:
            raise HTTPException(
                status_code=400,
                detail=f"无法启动：{why}（请调整风控参数或稍后再试）",
            )

        # 余额预检：引擎启动后立即检查余额，不足则自停
        bal_res = await db.execute(
            select(BookmakerAccount).where(
                BookmakerAccount.user_id == current_user.id,
                BookmakerAccount.code.in_(["ob", "pinnacle"]),
                BookmakerAccount.status == BookmakerStatus.CONNECTED,
            )
        )
        site_balances = [
            Decimal(str(acc.balance or 0))
            for acc in bal_res.scalars().all()
            if is_real_live_account(acc.code, acc.base_url or "")
        ]
        spendable = max(site_balances, default=Decimal("0"))
        if spendable <= 0:
            spendable = Decimal(str(current_user.balance or 0))
        min_balance = Decimal(str(getattr(settings, "AI_MIN_BALANCE", 10) or 10))
        if spendable < min_balance:
            raise HTTPException(
                status_code=400,
                detail=f"无法启动：余额不足（{spendable} < {min_balance}）",
            )

        start_result = await start_user_engine(current_user.id)
        status_info = await get_engine_status(current_user.id)
        if not status_info.get("running"):
            logger.error("AI 引擎启动未确认: user=%s result=%s", current_user.id, start_result)
            raise HTTPException(status_code=503, detail="AI 引擎未确认启动，请稍后重试")
        result = compose_ai_runtime_status(
            status_info=status_info,
            user=current_user,
            manual_analysis_sports=[],
        )
        await db.commit()
        message = "自动下单已在运行" if start_result.get("status") == "already_running" else "自动下单已启动"
        if start_result.get("status") == "recovered_stale_lock":
            message += "，已清理失效引擎锁"
        if manual_analysis_sports:
            message += "，已停止手动分析"
        return APIResponse(message=message, data=result)
    else:
        manual_analysis_sports = await list_analysis_watching_sports(current_user.id)
        await stop_user_engine(current_user.id)
        result = compose_ai_runtime_status(
            status_info=await get_engine_status(current_user.id),
            user=current_user,
            manual_analysis_sports=manual_analysis_sports,
        )
        await db.commit()
        return APIResponse(message="自动下单已停止", data=result)


@router.post("/stop", response_model=APIResponse)
async def stop_ai(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """停止AI引擎"""
    from app.ai.recs_job import list_analysis_watching_sports

    current_user.ai_enabled = False
    await db.flush()
    await stop_user_engine(current_user.id)
    manual_analysis_sports = await list_analysis_watching_sports(current_user.id)
    result = compose_ai_runtime_status(
        status_info=await get_engine_status(current_user.id),
        user=current_user,
        manual_analysis_sports=manual_analysis_sports,
    )
    await db.commit()
    return APIResponse(message="自动下单已停止", data=result)


@router.get("/status", response_model=APIResponse)
async def ai_status(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """获取AI引擎运行状态（含人工/自动开关）"""
    from app.ai.recs_job import list_analysis_watching_sports

    fresh = await db.get(User, current_user.id)
    status_info = await get_engine_status(current_user.id)
    ai_enabled = bool(getattr(fresh or current_user, "ai_enabled", False))
    # 状态查询必须无副作用。引擎会在下一轮自行读取持久化开关并退出；
    # 此处仅屏蔽已关闭 AI 的过期跨 worker 标记，避免刷新页面改变运行状态。
    if not ai_enabled and bool(status_info.get("running")):
        status_info = {**status_info, "running": False}
    runtime = compose_ai_runtime_status(
        status_info=status_info,
        user=fresh or current_user,
        manual_analysis_sports=await list_analysis_watching_sports(current_user.id),
    )
    return APIResponse(data=runtime)


# === AI推荐 ===
@router.get("/recommend/{match_id}", response_model=APIResponse)
async def get_recommendation(
    match_id: int,
    current_user: User = Depends(get_current_user),
):
    """获取 AI 全场小球投注建议。"""
    result = await analyze_and_recommend(
        match_id, current_user.id
    )
    return APIResponse(data=result)


# === 批量推荐 ===
@router.get("/recommendations", response_model=APIResponse)
async def get_batch_recommendations(
    sport: str = "football",
    mode: str = "single",
    provider: str = "",
    limit: int = 10,
    refresh: bool = False,
    current_user: User = Depends(get_current_user),
):
    """AI 批量推荐：读缓存；仅在「开始分析」后后台轮询；胜率≥策略阈值才展示。"""
    from app.ai.recs_job import (
        ensure_recs_job,
        filter_recs_by_bet_mode,
        get_job_snapshot,
        is_analysis_watching,
        list_live_match_ids,
        recs_cache_key,
    )
    from app.ai.strategy import load_fresh_strategy
    from app.core.cache import cache
    from app.services.bet_mode import get_user_bet_mode
    from app.services.bookmakers.sport_classify import normalize_sport

    _ = mode
    position_mode = "single"
    provider_code = (provider or "").strip().lower()
    if provider_code not in ("ob", "pinnacle", ""):
        provider_code = ""

    sport_norm = normalize_sport(sport)
    if sport_norm not in ("football", "basketball"):
        return APIResponse(
            success=False,
            message="仅支持足球或篮球",
            data=None,
        )

    from app.config import settings as _settings

    default_lim = int(getattr(_settings, "AI_RECS_LIMIT", 80) or 80)
    scan_interval_sec = max(120, int(getattr(_settings, "AI_SCAN_INTERVAL_SEC", 120) or 120))
    limit = max(1, min(int(limit or default_lim), _settings.AI_RECS_MAX_LIMIT))
    bet_mode = get_user_bet_mode(current_user)
    ai_snap, strat_cfg = await load_fresh_strategy(current_user.id)
    min_conf = float(getattr(strat_cfg, "min_confidence", 0.0) or 0.0)
    min_odds = float(getattr(strat_cfg, "min_odds", 1.1) or 1.1)
    max_odds = float(getattr(strat_cfg, "max_odds", 10.0) or 10.0)
    preferred_sports = list(getattr(ai_snap, "preferred_sports", None) or [])
    excluded_teams = list(getattr(ai_snap, "excluded_teams", None) or [])
    watching = await is_analysis_watching(current_user.id, sport_norm)

    cache_key = recs_cache_key(current_user.id, sport_norm, provider_code)
    cached = await cache.get_json(cache_key)
    job = await get_job_snapshot(current_user.id, sport_norm, provider_code)

    # 仅在用户已「开始分析」时自动拉起/续跑；refresh 且未 watch 时只读缓存
    has_cache = isinstance(cached, dict) and "recommendations" in cached
    if watching and (
        refresh
        or (
            not has_cache
            and job.get("status") not in ("starting", "analyzing")
        )
    ):
        job = await ensure_recs_job(
            user_id=current_user.id,
            sport=sport_norm,
            provider=provider_code,
            limit=limit,
            force=bool(refresh),
        )
    else:
        job = await get_job_snapshot(current_user.id, sport_norm, provider_code)
        watching = bool(job.get("analysis_enabled"))

    # 估算赛事页候选数（轻量查询，供进度展示）
    try:
        live_ids = await list_live_match_ids(
            sport=sport_norm,
            provider=provider_code,
            limit=limit,
            user_id=current_user.id,
            min_odds=min_odds,
            max_odds=max_odds,
        )
        live_total = len(live_ids)
    except Exception:
        live_total = int(job.get("total") or 0)

    analyzing = job.get("status") in ("starting", "analyzing")
    filter_hint = (
        f"分析中 · 每 {scan_interval_sec} 秒扫描滚球 · 置信度≥{min_conf * 100:.0f}% · 赔率[{min_odds:g},{max_odds:g}]"
        if watching
        else f"点击开始分析后，系统会每 {scan_interval_sec} 秒扫描滚球，达到置信度≥{min_conf * 100:.0f}% 才显示"
    )

    if isinstance(cached, dict) and "recommendations" in cached:
        raw_list = list(cached.get("recommendations") or [])
        from app.ai.analysis_filters import enrich_recs_skip_from_db

        # 先按库内最新比分/时钟/配置赔率区间过滤，再按完整策略过滤
        guarded = await enrich_recs_skip_from_db(
            raw_list, min_odds=min_odds, max_odds=max_odds
        )
        filtered = filter_recs_by_bet_mode(
            guarded,
            bet_mode=bet_mode,
            min_confidence=min_conf,
            min_odds=min_odds,
            max_odds=max_odds,
            preferred_sports=preferred_sports,
            excluded_teams=excluded_teams,
            strat=strat_cfg,
        )
        payload = dict(cached)
        payload.update(
            {
                "recommendations": filtered,
                "count": len(filtered),
                "raw_count": len(raw_list),
                "status": "analyzing" if analyzing else payload.get("status") or "ready",
                "progress": job.get("progress") or payload.get("progress") or 0,
                "total": job.get("total") or live_total or payload.get("total") or 0,
                "job_status": job.get("status") or "idle",
                "analysis_enabled": watching,
                "source": "cache" if not analyzing else "cache_updating",
                "sport": sport_norm,
                "mode": position_mode,
                "bet_mode": bet_mode,
                "filter": "simple",
                "min_win_rate": round(min_conf * 100, 1),
                "provider": provider_code or None,
                "scope": "live",
                "market": "total",
                "hint": (
                    None
                    if filtered
                    else f"暂无置信度≥{min_conf * 100:.0f}% 的比赛"
                ),
                "filter_hint": filter_hint,
            }
        )
        return APIResponse(
            data=payload,
            message="分析中" if analyzing else "已更新",
        )

    hint = (
        "正在分析滚球，完成后会自动显示推荐"
        if watching or analyzing
        else "点击开始分析后会自动轮询滚球"
    )
    if job.get("status") == "error":
        hint = f"分析失败：{job.get('error') or '未知错误'}"
    elif live_total == 0 and not analyzing:
        hint = "暂无滚球，请先同步站点赛事"

    return APIResponse(
        data={
            "count": 0,
            "recommendations": [],
            "sport": sport_norm,
            "bet_mode": bet_mode,
            "filter": "simple",
            "min_win_rate": round(min_conf * 100, 1),
            "filter_hint": filter_hint,
            "analysis_enabled": watching,
            "provider": provider_code or None,
            "scope": "live",
            "market": "total",
            "status": "analyzing" if analyzing else ("ready" if live_total == 0 else "idle"),
            "progress": job.get("progress") or 0,
            "total": job.get("total") or live_total,
            "job_status": job.get("status") or "idle",
            "source": "pending",
            "hint": hint,
        },
        message="分析中" if analyzing else "待分析",
    )


@router.post("/recommendations/start", response_model=APIResponse)
async def start_recommendations_analysis(
    sport: str = "football",
    limit: int = 80,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """开始后台轮询分析当前球类滚球小球。"""
    from app.ai.recs_job import start_analysis_watch
    from app.config import settings as _settings
    from app.ai.strategy import load_fresh_strategy
    from app.services.bet_mode import get_user_bet_mode
    from app.services.bookmakers.sport_classify import normalize_sport

    sport_norm = normalize_sport(sport)
    if sport_norm not in ("football", "basketball"):
        return APIResponse(success=False, message="仅支持足球或篮球")
    fresh_user = await db.get(User, current_user.id)
    if get_user_bet_mode(fresh_user or current_user) != "manual":
        return APIResponse(success=False, message="自动模式下不能启动手动分析，请先切换到人工")
    if bool(getattr(fresh_user or current_user, "ai_enabled", False)):
        return APIResponse(success=False, message="请先停止自动下单，再启动手动分析")
    ai_snap, _ = await load_fresh_strategy(current_user.id)
    if ai_snap and not getattr(ai_snap, "is_active", True):
        return APIResponse(success=False, message="请先在 AI 设置中开启 AI")
    default_lim = int(getattr(_settings, "AI_RECS_LIMIT", 80) or 80)
    limit = max(1, min(int(limit or default_lim), _settings.AI_RECS_MAX_LIMIT))
    snap = await start_analysis_watch(
        user_id=current_user.id, sport=sport_norm, limit=limit
    )
    return APIResponse(data=snap, message="分析已开始")


@router.post("/recommendations/stop", response_model=APIResponse)
async def stop_recommendations_analysis(
    sport: str = "football",
    current_user: User = Depends(get_current_user),
):
    """停止后台分析。"""
    from app.ai.recs_job import stop_analysis_watch
    from app.services.bookmakers.sport_classify import normalize_sport

    sport_norm = normalize_sport(sport)
    if sport_norm not in ("football", "basketball"):
        return APIResponse(success=False, message="仅支持足球或篮球")
    snap = await stop_analysis_watch(user_id=current_user.id, sport=sport_norm)
    return APIResponse(data=snap, message="分析已停止")

# === AI投注历史 ===
@router.get("/history", response_model=APIResponse)
async def ai_bet_history(
    page: int = 1,
    page_size: int = 20,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """查询AI投注记录"""
    from app.models.user import Bet
    from sqlalchemy import select, desc

    offset = (page - 1) * page_size
    result = await db.execute(
        select(Bet)
        .where(Bet.user_id == current_user.id, Bet.is_ai_bet == True)
        .order_by(desc(Bet.created_at))
        .offset(offset).limit(page_size)
    )
    bets = result.scalars().all()

    items = [
        {
            "id": b.id,
            "match_id": b.match_id,
            "selection": b.selection,
            "odds": b.odds,
            "stake": float(b.stake),
            "status": b.status.value if hasattr(b.status, 'value') else str(b.status),
            "confidence": b.ai_confidence,
            "reasoning": b.ai_reasoning,
            "created_at": b.created_at.isoformat() if b.created_at else None,
        }
        for b in bets
    ]

    return APIResponse(data={"items": items, "page": page, "page_size": page_size})


# === 一键投注（各盘口独立下注，互不影响） ===
class OneClickBetRequest(BaseModel):
    stake: float = settings.AI_DEFAULT_STAKE


@router.post("/one-click-bet/{match_id}", response_model=APIResponse)
async def one_click_bet(
    match_id: int,
    req: OneClickBetRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """一键投注：OB/平博单边全场小球。

    与自动投注使用同一套 execute_bet 执行逻辑：
    跨站比价 → provider 解析 → 未连接自动切站 → 动态仓位 → 重试下单。
    """
    result = await db.execute(select(Match).where(Match.id == match_id))
    match = result.scalar_one_or_none()
    if not match:
        raise HTTPException(status_code=404, detail="赛事不存在")

    # 优先使用「开始分析」缓存的可投推荐，避免一键复检与列表不一致
    rec = None
    try:
        from app.core.cache import cache
        from app.ai.recs_job import recs_cache_key
        from app.services.bookmakers.sport_classify import normalize_sport

        sport_n = normalize_sport(getattr(match, "sport", None) or "football")
        for prov in ("ob", "pinnacle", ""):
            cached = await cache.get_json(recs_cache_key(current_user.id, sport_n, prov))
            if not isinstance(cached, dict):
                continue
            for item in cached.get("recommendations") or []:
                if int(item.get("match_id") or 0) != int(match_id):
                    continue
                r = item.get("recommendation") or {}
                if r.get("should_bet"):
                    rec = item
                    break
            if rec:
                break
    except Exception:
        rec = None

    if rec is None:
        try:
            rec = await analyze_and_recommend(match_id, current_user.id)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"AI分析失败: {e}")

    if rec.get("error"):
        raise HTTPException(status_code=400, detail=str(rec["error"]))

    from app.ai.strategy_gates import gate_recommendation_for_place, stake_bounds
    from app.ai.strategy import load_fresh_strategy, BetDecision

    _, strat0 = await load_fresh_strategy(current_user.id)
    lo, hi = stake_bounds(strat0)
    stake = Decimal(str(req.stake or 0))
    if stake <= 0:
        stake = lo
    if stake + Decimal("0.0001") < lo:
        raise HTTPException(status_code=400, detail=f"金额需 ≥{lo:g}（AI 策略配置）")
    if stake - Decimal("0.0001") > hi:
        raise HTTPException(
            status_code=400,
            detail=f"金额超过策略单笔上限 {hi:g}，请在配置中调整「单笔最大金额」",
        )

    ok_gate, why_gate, stake, _ = await gate_recommendation_for_place(
        user_id=current_user.id, rec=rec, stake=stake, db=db
    )
    if not ok_gate:
        raise HTTPException(status_code=400, detail=f"未通过策略配置: {why_gate}")

    # ── 从推荐结果构造 BetDecision，调用统一下单执行器 ──
    r = rec.get("recommendation") or {}
    if not r.get("should_bet"):
        raise HTTPException(status_code=400, detail="策略未通过，不可下单")

    sel = str(r.get("selection") or "").lower()
    if not sel:
        raise HTTPException(status_code=400, detail="推荐结果缺少投注方向")

    decision = BetDecision(
        match_id=match_id,
        selection=sel,
        confidence=float(r.get("confidence") or 0),
        suggested_stake=stake,
        reasoning=str(r.get("reasoning") or ""),
        risk_score=0.0,
        should_bet=True,
        bet_type=str(r.get("bet_type") or "total").lower(),
        provider_code=str(r.get("provider_code") or "").lower(),
        odds=float(r.get("odds") or 0),
        line=r.get("line"),
        sport=str(rec.get("sport") or ""),
    )

    from app.ai.bet_executor import execute_bet

    bet_result = await execute_bet(
        db, current_user, match, decision, strat0,
        is_auto=False,
    )

    if not bet_result.ok:
        raise HTTPException(status_code=400, detail=bet_result.message or "下单失败")

    return APIResponse(
        message="已提交真实站点投注",
        data={
            "bets": [{
                "market": decision.bet_type,
                "selection": sel,
                "odds": bet_result.odds,
                "stake": float(bet_result.stake),
                "provider": bet_result.provider_label,
                "provider_code": bet_result.provider_code,
                "status": "success",
                "potential_payout": float(bet_result.potential_payout),
                "site_balance": bet_result.site_balance,
                "external_bet_id": bet_result.external_bet_id,
                "bet_id": bet_result.bet_id,
            }],
            "failed": [],
            "total_stake": float(bet_result.stake),
            "success_count": 1,
            "failed_count": 0,
            "provider": bet_result.provider_label,
        },
    )
