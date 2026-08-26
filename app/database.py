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
    echo=False,
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
    """依赖注入：获取数据库会话。

    长时间操作（如 nowscore 爬取）期间，PostgreSQL 的
    idle_in_transaction_session_timeout 可能关闭底层连接。
    此处对 commit 失败做容错：连接已关闭时静默丢弃该会话，不中断请求。
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            # 路由可在长时间浏览器/AI 操作前自行提交并释放事务。
            # 此处仅提交仍在进行的事务，避免对已被连接池回收的连接重复 commit。
            if session.in_transaction():
                try:
                    await session.commit()
                except Exception as commit_exc:
                    # 连接已被服务端关闭（idle_in_transaction_session_timeout 等），
                    # 事务无法提交但不影响已完成的业务逻辑。
                    logger.debug("get_db commit 容错（连接可能已关闭）: %s", commit_exc)
                    try:
                        await session.rollback()
                    except Exception:
                        pass  # 连接已关，rollback 也会失败
        except Exception:
            try:
                if session.in_transaction():
                    await session.rollback()
            except Exception:
                pass  # 连接已关，rollback 失败也忽略
            raise
        finally:
            try:
                await session.close()
            except Exception:
                pass  # 连接已关，close 也可能失败


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
        "ALTER TABLE odds ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMP WITHOUT TIME ZONE",
        "UPDATE odds SET last_seen_at = COALESCE(last_seen_at, valid_from, created_at) WHERE last_seen_at IS NULL",
        "ALTER TABLE odds ALTER COLUMN last_seen_at SET NOT NULL",
        "CREATE INDEX IF NOT EXISTS ix_odds_last_seen_at ON odds (last_seen_at)",
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
    """把历史 AI 策略值统一收敛到唯一的 simple。"""
    inspector = inspect(sync_conn)
    if "ai_configs" not in set(inspector.get_table_names()):
        return

    try:
        sync_conn.execute(text(
            """
            UPDATE ai_configs
            SET strategy = 'simple'
            WHERE strategy IS NULL
               OR TRIM(strategy) = ''
               OR LOWER(TRIM(strategy)) <> 'simple'
            """
        ))
    except Exception as exc:
        logger.warning("清洗 ai_configs.strategy 失败: %s", exc)

    if sync_conn.dialect.name != "postgresql":
        return

    # 删除旧的 CHECK 约束，统一为 simple
    postgres_statements = [
        "ALTER TABLE ai_configs ALTER COLUMN strategy SET DEFAULT 'simple'",
        "UPDATE ai_configs SET strategy = 'simple' WHERE strategy IS NULL",
        "ALTER TABLE ai_configs ALTER COLUMN strategy SET NOT NULL",
        "ALTER TABLE ai_configs DROP CONSTRAINT IF EXISTS ck_ai_configs_strategy_high_win_rate",
    ]
    for sql in postgres_statements:
        try:
            sync_conn.execute(text(sql))
        except Exception as exc:
            logger.warning("执行 ai_configs.strategy 线上迁移失败: %s", exc)


async def close_db():
    """关闭数据库连接"""
    await engine.dispose()
