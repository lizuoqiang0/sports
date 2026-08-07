"""
AI 自动投注引擎 - docker-compose ai-engine 服务入口。

作为后台守护进程持续扫描与投注；下单一律走体育站真实接口，
是否自动执行由用户 bet_mode（人工/自动）控制。

由 docker-compose 启动：
    docker compose --profile ai up -d ai-engine
等价于: python scripts/ai_betting_engine.py --all-users --live
"""
import asyncio
import argparse
import logging
import signal
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import AsyncSessionLocal, init_db
from app.models.user import User
from app.ai.auto_better import AIBettingEngine
from app.config import settings

os.makedirs(os.path.dirname(settings.LOG_FILE) or ".", exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(settings.LOG_FILE, encoding="utf-8"),
    ],
)
logger = logging.getLogger("ai.engine")


async def run_single_user(user_id: int):
    """为单个用户运行 AI 引擎"""
    async with AsyncSessionLocal() as db:
        result = await db.execute(User.__table__.select().where(User.id == user_id))
        user = result.first()
        if not user:
            logger.error(f"用户不存在: id={user_id}")
            return
        logger.info(f"启动 AI 引擎: user={user.username} (id={user_id})")

    engine = AIBettingEngine(user_id)
    await engine.start()

    stop_event = asyncio.Event()

    def signal_handler():
        logger.info("收到停止信号...")
        stop_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, signal_handler)

    await stop_event.wait()
    await engine.stop()
    logger.info("AI 引擎已停止")


async def run_all_users():
    """为所有启用 AI 的用户运行"""
    async with AsyncSessionLocal() as db:
        result = await db.execute(User.__table__.select().where(User.ai_enabled == True))
        users = result.fetchall()
        if not users:
            logger.warning("没有启用 AI 的用户")
            return
        logger.info(f"为 {len(users)} 个用户启动 AI 引擎")

        engines = []
        for user_row in users:
            engine = AIBettingEngine(user_row.id)
            await engine.start()
            engines.append(engine)

        stop_event = asyncio.Event()
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: stop_event.set())

        await stop_event.wait()
        for eng in engines:
            await eng.stop()
        logger.info("所有 AI 引擎已停止")


async def main():
    parser = argparse.ArgumentParser(description="OB Sports AI 投注引擎")
    parser.add_argument("--user-id", type=int, help="指定用户 ID")
    parser.add_argument("--all-users", action="store_true", help="为所有启用 AI 的用户运行")
    parser.add_argument("--live", action="store_true", help="实盘模式（默认即为真实下单，保留以兼容编排参数）")
    args = parser.parse_args()

    await init_db()

    if args.all_users:
        await run_all_users()
    elif args.user_id:
        await run_single_user(args.user_id)
    else:
        parser.error("请指定 --all-users 或 --user-id")


if __name__ == "__main__":
    asyncio.run(main())
