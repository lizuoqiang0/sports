"""
数据库引擎与会话管理
"""
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from contextlib import asynccontextmanager
from app.config import settings

# 异步引擎
engine = create_async_engine(
    settings.DATABASE_URL,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_timeout=settings.DB_POOL_TIMEOUT,
    pool_recycle=settings.DB_POOL_RECYCLE,
    pool_pre_ping=True,
    pool_use_lifo=True,  # 热连接优先，降低冷连接延迟
    echo=settings.DEBUG,
)

# 会话工厂
AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    """ORM 模型基类"""
    pass


async def get_db() -> AsyncSession:
    """依赖注入：获取数据库会话"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def init_db():
    """初始化数据库表，并补齐增量列"""
    # 确保模型已注册到 metadata
    import app.models.user  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_migrate_columns)


def _migrate_columns(sync_conn):
    """兼容已有库：补齐 bets 新列与枚举值"""
    from sqlalchemy import text

    statements = [
        "ALTER TABLE bets ADD COLUMN IF NOT EXISTS provider VARCHAR(50) DEFAULT 'OB体育'",
        "ALTER TABLE bets ADD COLUMN IF NOT EXISTS line DOUBLE PRECISION",
        "ALTER TABLE bets ADD COLUMN IF NOT EXISTS external_bet_id VARCHAR(120)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_bets_user_provider_ext ON bets (user_id, provider, external_bet_id) WHERE external_bet_id IS NOT NULL AND external_bet_id <> ''",
        "ALTER TABLE bookmaker_accounts ADD COLUMN IF NOT EXISTS session_token_encrypted TEXT DEFAULT ''",
        "ALTER TABLE bookmaker_accounts ADD COLUMN IF NOT EXISTS profile_json JSONB DEFAULT '{}'::jsonb",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS bet_mode VARCHAR(20) DEFAULT 'manual'",
        # Postgres 枚举增量
        "DO $$ BEGIN ALTER TYPE sporttype ADD VALUE IF NOT EXISTS 'VOLLEYBALL'; EXCEPTION WHEN duplicate_object THEN NULL; END $$",
        "DO $$ BEGIN ALTER TYPE sporttype ADD VALUE IF NOT EXISTS 'TABLE_TENNIS'; EXCEPTION WHEN duplicate_object THEN NULL; END $$",
        "DO $$ BEGIN ALTER TYPE sporttype ADD VALUE IF NOT EXISTS 'BADMINTON'; EXCEPTION WHEN duplicate_object THEN NULL; END $$",
        "DO $$ BEGIN ALTER TYPE sporttype ADD VALUE IF NOT EXISTS 'SNOOKER'; EXCEPTION WHEN duplicate_object THEN NULL; END $$",
        "DO $$ BEGIN ALTER TYPE sporttype ADD VALUE IF NOT EXISTS 'HOCKEY'; EXCEPTION WHEN duplicate_object THEN NULL; END $$",
        "DO $$ BEGIN ALTER TYPE sporttype ADD VALUE IF NOT EXISTS 'AMERICAN_FOOTBALL'; EXCEPTION WHEN duplicate_object THEN NULL; END $$",
        "DO $$ BEGIN ALTER TYPE sporttype ADD VALUE IF NOT EXISTS 'OTHER'; EXCEPTION WHEN duplicate_object THEN NULL; END $$",
    ]
    for sql in statements:
        try:
            sync_conn.execute(text(sql))
        except Exception:
            pass


async def close_db():
    """关闭数据库连接"""
    await engine.dispose()
