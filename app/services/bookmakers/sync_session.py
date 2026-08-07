"""
同步会话助手：凭证快照 → 释放连接 → Gate I/O → 短写会话。

长耗时 Browser Gate 调用期间不占用 AsyncSession，避免 connection is closed。
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt_secret
from app.models.user import BookmakerAccount


def snapshot_account(acc: BookmakerAccount) -> dict[str, Any]:
    """提取账户凭证快照（纯 Python dict，不持有 ORM）。"""
    return {
        "id": int(acc.id),
        "code": str(acc.code or ""),
        "name": getattr(acc, "name", None),
        "base_url": acc.base_url or "",
        "username": acc.username or "",
        "password": decrypt_secret(acc.password_encrypted),
        "balance": acc.balance,
        "session_token": decrypt_secret(acc.session_token_encrypted),
        "profile": dict(acc.profile_json) if isinstance(acc.profile_json, dict) else {},
    }


async def release_db_session(db: AsyncSession) -> None:
    """提交或回滚以释放连接池中的连接。"""
    try:
        await db.commit()
    except Exception:
        try:
            await db.rollback()
        except Exception:
            pass


async def with_write_session(
    fn: Callable[[AsyncSession], Awaitable[Any]],
) -> Any:
    """打开短写会话执行 fn，成功则 commit。"""
    from app.database import AsyncSessionLocal

    async with AsyncSessionLocal() as wdb:
        result = await fn(wdb)
        await wdb.commit()
        return result
