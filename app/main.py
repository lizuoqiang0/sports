"""
OB Sports Betting Platform - FastAPI 主入口
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from sqlalchemy.exc import SQLAlchemyError

from app.config import settings
from app.database import init_db, close_db
from app.core.cache import cache
from app.core.websocket import manager

# API路由
from app.api import auth, matches, odds, bets, ai_bets, admin, bookmakers, monitoring

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL),
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
logger = logging.getLogger("ob.main")

_WEAK_SECRETS = {
    "your-super-secret-key-change-in-production-min-32-chars",
    "ob-prod-please-override-with-openssl-rand-hex-32",
    "changeme",
    "secret",
    "ob-internal",
    "ob_password",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动/关闭钩子"""
    logger.info("%s v%s starting (production)", settings.APP_NAME, settings.APP_VERSION)

    if settings.SECRET_KEY in _WEAK_SECRETS or len(settings.SECRET_KEY or "") < 32:
        logger.error("禁止弱/过短 SECRET_KEY，请用 openssl rand -hex 32")
        raise RuntimeError("weak SECRET_KEY blocked")
    token = (settings.INTERNAL_API_TOKEN or "").strip()
    if not token or token in _WEAK_SECRETS:
        logger.error("必须配置强 INTERNAL_API_TOKEN（Backend↔Gate 鉴权）")
        raise RuntimeError("weak INTERNAL_API_TOKEN blocked")

    # 连接Redis
    try:
        await cache.connect()
        # 清理上次进程残留的 AI 引擎运行标记（进程重启后引擎已不存在）
        import redis.asyncio as aioredis
        keys = await cache.client.keys("ai:engine:running:*")
        if keys:
            await cache.client.delete(*keys)
            logger.info("清理残留 AI 引擎标记: %s 个", len(keys))
    except Exception as e:
        logger.warning(f"Redis连接失败(继续运行): {e}")

    # 初始化数据库
    try:
        await init_db()
        logger.info("✅ 数据库初始化完成")
    except Exception as e:
        logger.exception("数据库初始化失败，服务终止启动: %s", e)
        raise

    # 启动WebSocket心跳 + 跨 worker 扇出
    import asyncio
    asyncio.create_task(manager.heartbeat_loop())
    try:
        await manager.start_fanout()
    except Exception as e:
        logger.warning(f"WS fanout 启动失败: {e}")

    # 多 worker：仅 leader 跑后台轮询，避免重复刮数
    from app.core.worker_leader import start_leader_election, is_background_leader

    start_leader_election()
    await asyncio.sleep(0.3)  # 给一次抢主机会

    run_jobs = bool(settings.RUN_BACKGROUND_JOBS)
    is_leader = is_background_leader() if run_jobs else False
    if run_jobs and not is_leader:
        # 单 worker 时选举几乎立即成功；多 worker 稍后由监督任务接管
        logger.info("本进程暂非 background leader，轮询交由 leader 执行")

    async def _supervise_background_jobs() -> None:
        """跟随 leader 身份启停 live/balance poller。"""
        live_on = False
        bal_on = False
        ctx_on = False
        ns_on = False
        settle_on = False
        while True:
            try:
                lead = bool(settings.RUN_BACKGROUND_JOBS) and is_background_leader()
                if lead and not settle_on:
                    try:
                        from app.services.bet_settlement import start_settlement_worker
                        start_settlement_worker()
                        settle_on = True
                        logger.info("bet settlement worker started (leader)")
                    except Exception as e:
                        logger.warning(f"bet settlement worker 启动失败: {e}")
                if lead and not live_on:
                    try:
                        from app.services.bookmakers.live_poller import start_live_poller
                        start_live_poller()
                        live_on = True
                        logger.info("live poller started (leader)")
                    except Exception as e:
                        logger.warning(f"live poller 启动失败: {e}")
                if lead and not bal_on:
                    try:
                        from app.services.bookmakers.balance_poller import start_balance_poller
                        start_balance_poller()
                        bal_on = True
                        logger.info("balance poller started (leader)")
                    except Exception as e:
                        logger.warning(f"balance poller 启动失败: {e}")
                if lead and not ctx_on:
                    try:
                        from app.services.context_prefetcher import start_context_prefetcher
                        start_context_prefetcher()
                        ctx_on = True
                        logger.info("context prefetcher started (leader)")
                    except Exception as e:
                        logger.warning(f"context prefetcher 启动失败: {e}")
                if lead and not ns_on:
                    try:
                        from app.services.nowscore_prefetcher import start_nowscore_prefetcher
                        start_nowscore_prefetcher()
                        ns_on = True
                        logger.info("nowscore prefetcher started (leader)")
                    except Exception as e:
                        logger.warning(f"nowscore prefetcher 启动失败: {e}")
                if not lead and live_on:
                    try:
                        from app.services.bookmakers.live_poller import stop_live_poller
                        stop_live_poller()
                    except Exception:
                        pass
                    live_on = False
                if not lead and bal_on:
                    try:
                        from app.services.bookmakers.balance_poller import stop_balance_poller
                        stop_balance_poller()
                    except Exception:
                        pass
                    bal_on = False
                if not lead and ctx_on:
                    try:
                        from app.services.context_prefetcher import stop_context_prefetcher
                        stop_context_prefetcher()
                    except Exception:
                        pass
                    ctx_on = False
                if not lead and ns_on:
                    try:
                        from app.services.nowscore_prefetcher import stop_nowscore_prefetcher
                        stop_nowscore_prefetcher()
                    except Exception:
                        pass
                    ns_on = False
                if not lead and settle_on:
                    try:
                        from app.services.bet_settlement import stop_settlement_worker
                        stop_settlement_worker()
                    except Exception:
                        pass
                    settle_on = False
            except Exception:
                logger.exception("background job supervisor error")
            await asyncio.sleep(5)

    bg_task = asyncio.create_task(_supervise_background_jobs(), name="ob-bg-supervisor")

    # 启动数据清理任务（仅 leader 执行）
    if run_jobs and is_leader:
        from app.services.cleanup import start_cleanup_task
        start_cleanup_task()
        logger.info("cleanup task started (leader)")

    yield

    # 关闭
    logger.info("🛑 正在关闭...")
    bg_task.cancel()
    try:
        await bg_task
    except asyncio.CancelledError:
        pass
    try:
        from app.services.cleanup import stop_cleanup_task
        stop_cleanup_task()
    except Exception:
        pass
    try:
        from app.core.worker_leader import stop_leader_election
        stop_leader_election()
    except Exception:
        pass
    try:
        from app.services.bookmakers.live_poller import stop_live_poller
        stop_live_poller()
    except Exception:
        pass
    try:
        from app.services.bookmakers.balance_poller import stop_balance_poller
        stop_balance_poller()
    except Exception:
        pass
    try:
        from app.services.context_prefetcher import stop_context_prefetcher
        stop_context_prefetcher()
    except Exception:
        pass
    try:
        from app.services.nowscore_prefetcher import stop_nowscore_prefetcher
        stop_nowscore_prefetcher()
    except Exception:
        pass
    try:
        await manager.stop_fanout()
    except Exception:
        pass
    await cache.close()
    await close_db()
    logger.info("👋 已安全关闭")


app = FastAPI(
    title="OB Sports Betting Platform",
    description="体育赔率监控与 OB/平博单边投注平台",
    version=settings.APP_VERSION,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)

# === 中间件 ===
from fastapi.middleware.gzip import GZipMiddleware

app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID", "X-Internal-Token"],
)
_hosts = list(settings.ALLOWED_HOSTS or [])
if "*" not in _hosts:
    for h in ("localhost", "127.0.0.1", "backend", "ob-backend"):
        if h not in _hosts:
            _hosts.append(h)
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=_hosts,
)

# === 异常处理 ===
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={"success": False, "message": "参数校验失败"},
    )

@app.exception_handler(SQLAlchemyError)
async def db_exception_handler(request: Request, exc: SQLAlchemyError):
    logger.error(f"数据库错误: {exc}")
    return JSONResponse(
        status_code=500,
        content={"success": False, "message": "数据库错误"},
    )

# === 健康检查 ===
@app.get("/health")
async def health_check():
    """健康检查端点"""
    return {
        "status": "healthy",
        "service": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "timestamp": __import__("datetime").datetime.now().isoformat(),
    }

@app.get("/ready")
async def readiness_check():
    """就绪检查 (检查DB/Redis连接)"""
    checks = {"database": "ok", "redis": "ok"}

    try:
        from app.database import engine
        async with engine.connect() as conn:
            await conn.execute(__import__("sqlalchemy").text("SELECT 1"))
    except Exception as e:
        checks["database"] = f"error: {e}"

    try:
        await cache.client.ping()
    except Exception as e:
        checks["redis"] = f"error: {e}"

    all_ok = all(v == "ok" for v in checks.values())
    return JSONResponse(
        status_code=200 if all_ok else 503,
        content={"ready": all_ok, "checks": checks},
    )

# === 注册路由 ===
app.include_router(auth.router)
app.include_router(matches.router)
app.include_router(odds.router)
app.include_router(bets.router)
app.include_router(ai_bets.router)
app.include_router(admin.router)
app.include_router(bookmakers.router)
app.include_router(monitoring.router)

# === 根路径 ===
@app.get("/")
async def root():
    payload = {
        "service": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "environment": "production",
        "endpoints": {
            "health": "/health",
            "ready": "/ready",
            "auth": "/api/v1/auth",
            "matches": "/api/v1/matches",
            "bets": "/api/v1/bets",
            "ai": "/api/v1/ai",
            "bookmakers": "/api/v1/bookmakers",
        },
    }
    return payload
