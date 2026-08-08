"""
数据库引擎与会话管理
"""
import logging

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from app.config import settings

logger = logging.getLogger("ob.database")

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
    statements = [
        "ALTER TABLE bets ADD COLUMN IF NOT EXISTS provider VARCHAR(50) DEFAULT 'OB体育'",
        "ALTER TABLE bets ADD COLUMN IF NOT EXISTS line DOUBLE PRECISION",
        "ALTER TABLE bets ADD COLUMN IF NOT EXISTS external_bet_id VARCHAR(120)",
        "ALTER TABLE bets ADD COLUMN IF NOT EXISTS actual_payout NUMERIC(18, 2) DEFAULT 0",
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
        except Exception as exc:
            logger.debug("跳过兼容迁移 SQL: %s (%s)", sql, exc)

    _migrate_ai_config_strategy(sync_conn)


def _migrate_ai_config_strategy(sync_conn) -> None:
    """把历史 AI 策略值统一收敛到唯一的 high_win_rate。"""
    inspector = inspect(sync_conn)
    if "ai_configs" not in set(inspector.get_table_names()):
        return

    try:
        sync_conn.execute(text(
            """
            UPDATE ai_configs
            SET strategy = 'high_win_rate'
            WHERE strategy IS NULL
               OR TRIM(strategy) = ''
               OR LOWER(TRIM(strategy)) <> 'high_win_rate'
            """
        ))
    except Exception as exc:
        logger.warning("清洗 ai_configs.strategy 失败: %s", exc)

    if sync_conn.dialect.name != "postgresql":
        return

    postgres_statements = [
        "ALTER TABLE ai_configs ALTER COLUMN strategy SET DEFAULT 'high_win_rate'",
        "UPDATE ai_configs SET strategy = 'high_win_rate' WHERE strategy IS NULL",
        "ALTER TABLE ai_configs ALTER COLUMN strategy SET NOT NULL",
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conname = 'ck_ai_configs_strategy_high_win_rate'
            ) THEN
                ALTER TABLE ai_configs
                ADD CONSTRAINT ck_ai_configs_strategy_high_win_rate
                CHECK (strategy = 'high_win_rate');
            END IF;
        END $$;
        """,
    ]
    for sql in postgres_statements:
        try:
            sync_conn.execute(text(sql))
        except Exception as exc:
            logger.warning("执行 ai_configs.strategy 线上迁移失败: %s", exc)


async def close_db():
    """关闭数据库连接"""
    await engine.dispose()
