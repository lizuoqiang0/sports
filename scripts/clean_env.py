#!/usr/bin/env python3
"""清空业务数据，留下干净可登录环境（生产维护脚本）。

保留：表结构 + 一个登录账号（通过环境变量 CLEAN_KEEP_USER / CLEAN_KEEP_PASSWORD 指定）
清除：赛事/赔率/注单/机会/交易/AI 配置/站点会话等
站点配置：OB/平博空行（无演示 URL、无会话、未连接）
"""
from __future__ import annotations

import asyncio
import os
import sys
from decimal import Decimal

# 容器内优先 /app；本机则为仓库根目录
_ROOT = os.environ.get("PYTHONPATH", "").split(":")[0] or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
)
if _ROOT and _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
if "/app" not in sys.path and os.path.isdir("/app/app"):
    sys.path.insert(0, "/app")

from sqlalchemy import delete, select, text

from app.core.security import hash_password
from app.database import AsyncSessionLocal, init_db
from app.models.user import (
    AIConfig,
    Bet,
    BookmakerAccount,
    BookmakerStatus,
    Match,
    Odds,
    Transaction,
    User,
    UserRole,
)
from sqlalchemy import text
from app.services.bookmakers.catalog import BOOKMAKER_CATALOG


KEEP_USERNAME = (os.getenv("CLEAN_KEEP_USER") or "").strip()
KEEP_PASSWORD = (os.getenv("CLEAN_KEEP_PASSWORD") or "").strip()
KEEP_BALANCE = Decimal(os.getenv("CLEAN_KEEP_BALANCE") or "0")

if not KEEP_USERNAME or not KEEP_PASSWORD:
    print("错误：必须设置环境变量 CLEAN_KEEP_USER 和 CLEAN_KEEP_PASSWORD", file=sys.stderr)
    sys.exit(1)


async def clean() -> dict:
    await init_db()
    stats: dict[str, int] = {}
    async with AsyncSessionLocal() as db:
        # 遗留监控表（模型已移除，用裸 SQL 清理）
        for tbl in (
            "execution_events",
            "execution_tasks",
            "simulation_runs",
            "quotes",
            "market_matches",
            "event_matches",
            "markets",
            "sport_events",
            "risk_events",
            "source_accounts",
            "data_sources",
        ):
            try:
                res = await db.execute(text(f"DELETE FROM {tbl}"))
                stats[tbl] = int(res.rowcount or 0)
            except Exception:
                stats[tbl] = 0

        # 业务表（模型仍在用）
        for model, key in (
            (Bet, "bets"),
            (Odds, "odds"),
            (Match, "matches"),
            (Transaction, "transactions"),
            (AIConfig, "ai_configs"),
            (BookmakerAccount, "bookmaker_accounts"),
        ):
            res = await db.execute(delete(model))
            stats[key] = int(res.rowcount or 0)

        # 用户：只保留一个干净账号
        users = (await db.execute(select(User))).scalars().all()
        kept = None
        for u in users:
            if u.username == KEEP_USERNAME:
                kept = u
            else:
                await db.delete(u)
                stats["users_deleted"] = stats.get("users_deleted", 0) + 1

        if kept:
            kept.email = kept.email or f"{KEEP_USERNAME}@ob-sports.com"
            kept.hashed_password = hash_password(KEEP_PASSWORD)
            kept.role = UserRole.USER
            kept.balance = KEEP_BALANCE
            kept.ai_enabled = False
            kept.is_active = True
            kept.is_verified = True
            kept.bet_mode = "manual"
            stats["user_kept"] = 1
        else:
            kept = User(
                username=KEEP_USERNAME,
                email=f"{KEEP_USERNAME}@ob-sports.com",
                hashed_password=hash_password(KEEP_PASSWORD),
                role=UserRole.USER,
                balance=KEEP_BALANCE,
                ai_enabled=False,
                is_active=True,
                is_verified=True,
            )
            db.add(kept)
            await db.flush()
            stats["user_created"] = 1

        await db.flush()

        # 空站点配置（真实场景：无演示 URL）
        for code, meta in BOOKMAKER_CATALOG.items():
            db.add(
                BookmakerAccount(
                    user_id=kept.id,
                    code=code,
                    name=meta["name"],
                    base_url="",
                    username="",
                    password_encrypted="",
                    session_token_encrypted="",
                    profile_json={},
                    status=BookmakerStatus.DISCONNECTED,
                    balance=Decimal("0"),
                    enabled=True,
                    last_error=None,
                    last_sync_at=None,
                )
            )
        stats["bookmaker_accounts_reset"] = len(BOOKMAKER_CATALOG)

        # 序列复位（可选，避免 ID 看起来像旧环境）
        try:
            await db.execute(
                text(
                    """
                    SELECT setval(pg_get_serial_sequence(t.relname::text, a.attname), 1, false)
                    FROM pg_class t
                    JOIN pg_attribute a ON a.attrelid = t.oid
                    JOIN pg_attrdef d ON d.adrelid = t.oid AND d.adnum = a.attnum
                    WHERE t.relkind = 'r' AND pg_get_serial_sequence(t.relname::text, a.attname) IS NOT NULL
                    """
                )
            )
        except Exception:
            pass

        await db.commit()
    return stats


async def main() -> None:
    stats = await clean()
    print("CLEAN_OK")
    for k, v in sorted(stats.items()):
        print(f"  {k}={v}")
    print(f"LOGIN user={KEEP_USERNAME} password={KEEP_PASSWORD} balance={KEEP_BALANCE}")


if __name__ == "__main__":
    asyncio.run(main())
