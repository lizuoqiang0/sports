"""
站点网站余额：现场拉取 + Gate 会话余额回退。

供 API / balance_poller 共用，避免 services → api 反向依赖。
"""
from __future__ import annotations

import asyncio
import logging
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt_secret
from app.models.user import BookmakerAccount, BookmakerStatus
from app.services.bookmakers.catalog import BOOKMAKER_CATALOG
from app.services.bookmakers.registry import get_connector

logger = logging.getLogger(__name__)

_SITE_ORDER = ("ob", "pinnacle")


async def gate_live_balances() -> dict[str, float]:
    """从 Browser Gate 长连接读取各站最近刮取到的真实余额。"""
    from app.services.bookmakers.gate_client import fetch_session_balances

    return await fetch_session_balances()


async def load_site_balances(db: AsyncSession, user_id: int) -> list[dict]:
    """读取 OB / 平博 真实网站余额（优先现场拉取，不用断开后的陈旧缓存冒充）。"""
    from app.services.bookmakers.sync import ensure_default_accounts

    await ensure_default_accounts(db, user_id)
    result = await db.execute(
        select(BookmakerAccount).where(BookmakerAccount.user_id == user_id)
    )
    by_code = {a.code: a for a in result.scalars().all()}
    gate_bals = await gate_live_balances()

    async def _refresh_one(acc: BookmakerAccount) -> dict:
        status = acc.status.value if hasattr(acc.status, "value") else str(acc.status)
        connected = status in (BookmakerStatus.CONNECTED.value, "connected", "CONNECTED")
        token = decrypt_secret(acc.session_token_encrypted) if acc.session_token_encrypted else ""
        old_bal = Decimal(str(acc.balance or 0))

        # 未连接且无 token：禁止用 Gate 共享会话余额冒充本账号（清零）
        if not connected and not token:
            if old_bal != 0:
                acc.balance = Decimal("0")
            return {"balance": 0.0, "live": False, "source": "none"}

        try:
            connector = get_connector(
                acc.code,
                base_url=acc.base_url,
                username=acc.username,
                password=decrypt_secret(acc.password_encrypted),
                balance=Decimal("0"),
                session_token=token,
                profile=acc.profile_json if isinstance(acc.profile_json, dict) else {},
            )
            # OB 侧栏刮取可能需等盘口锁释放（Gate 最长约等 18s）
            bal = await asyncio.wait_for(
                connector.fetch_balance(),
                timeout=35.0 if acc.code == "ob" else 12.0,
            )
            new_bal = Decimal(str(bal or 0))
            source = "site"
            profile = getattr(connector, "_profile", None) or {}
            confirmed = bool(profile.get("venue_balance_confirmed"))
            bal_src = str(profile.get("balance_source") or "")
            # 场馆侧栏/页明确读到 0.00：禁止再用 Gate/DB 正数缓存盖回去（OB 曾误把中心钱包 295 当场馆余额）
            venue_empty_ok = confirmed and new_bal <= 0 and bal_src in (
                "venue",
                "venue_empty",
                "venue_cache",
            )
            # 仅当本账号已连接/有 token 时，才允许回退本站 Gate 刮取值
            if (
                not venue_empty_ok
                and new_bal <= 0
                and connected
                and acc.code in gate_bals
                and float(gate_bals[acc.code]) > 0
            ):
                new_bal = Decimal(str(gate_bals[acc.code]))
                source = "gate"
            if (
                not venue_empty_ok
                and new_bal > 0
                and connected
                and float(gate_bals.get(acc.code) or 0) > 0
                and float(gate_bals[acc.code]) > float(new_bal)
            ):
                # Gate 顶栏若更大，优先采用（避免盘口页误刮小数字）
                gate_v = Decimal(str(gate_bals[acc.code]))
                if gate_v >= Decimal("10") or gate_v > new_bal * Decimal("2"):
                    new_bal = gate_v
                    source = "gate"
            # 防误刮：旧余额明显更大时，拒绝用「像赔率」的小额覆盖（如 16.78 → 1.0）
            if (
                not venue_empty_ok
                and old_bal >= Decimal("10")
                and new_bal > 0
                and new_bal < old_bal * Decimal("0.35")
                and new_bal <= Decimal("8")
            ):
                logger.warning(
                    "reject suspicious %s balance drop %s → %s (keep cache)",
                    acc.code,
                    old_bal,
                    new_bal,
                )
                return {"balance": float(old_bal), "live": False, "source": "cache"}
            if new_bal > 0:
                acc.balance = new_bal
                if isinstance(profile, dict) and profile:
                    merged = dict(acc.profile_json or {}) if isinstance(acc.profile_json, dict) else {}
                    merged.update(profile)
                    acc.profile_json = merged
                return {"balance": float(new_bal), "live": True, "source": source}
            # 现场确认为 0（侧栏 Hi 下方 0.00 / venue_empty）——覆盖错误的中心钱包缓存
            if connected and token and (venue_empty_ok or confirmed):
                acc.balance = Decimal("0")
                if isinstance(profile, dict) and profile:
                    merged = dict(acc.profile_json or {}) if isinstance(acc.profile_json, dict) else {}
                    merged.update(profile)
                    acc.profile_json = merged
                return {"balance": 0.0, "live": True, "source": "site"}
            if connected and token and source == "site":
                # 区分失败与真 0：未确认场馆源时保守不覆盖已有正数
                if old_bal > 0:
                    return {"balance": float(old_bal), "live": False, "source": "cache"}
                acc.balance = Decimal("0")
                return {"balance": 0.0, "live": False, "source": "site"}
            if old_bal > 0 and connected:
                return {"balance": float(old_bal), "live": False, "source": "cache"}
            acc.balance = Decimal("0")
            return {"balance": 0.0, "live": False, "source": "none"}
        except Exception as e:
            logger.debug("刷新 %s 余额失败: %s", acc.code, e)
            if connected and acc.code in gate_bals and float(gate_bals[acc.code]) > 0:
                live = Decimal(str(gate_bals[acc.code]))
                acc.balance = live
                return {"balance": float(live), "live": True, "source": "gate"}
            if connected and old_bal > 0:
                return {"balance": float(old_bal), "live": False, "source": "cache"}
            if not connected:
                acc.balance = Decimal("0")
            return {
                "balance": float(old_bal) if connected else 0.0,
                "live": False,
                "source": "cache" if connected else "none",
            }

    refresh_meta: dict[str, dict] = {}
    refresh_tasks = []
    codes_with_acc = []
    for code in _SITE_ORDER:
        acc = by_code.get(code)
        if acc:
            codes_with_acc.append(code)
            refresh_tasks.append(_refresh_one(acc))
    if refresh_tasks:
        results = await asyncio.gather(*refresh_tasks, return_exceptions=True)
        for code, res in zip(codes_with_acc, results):
            if isinstance(res, dict):
                refresh_meta[code] = res
            else:
                refresh_meta[code] = {"balance": 0.0, "live": False, "source": "error"}
        await db.flush()

    sites = []
    for code in _SITE_ORDER:
        meta = BOOKMAKER_CATALOG.get(code, {})
        acc = by_code.get(code)
        info = refresh_meta.get(code) or {}
        if not acc:
            sites.append({
                "code": code,
                "name": meta.get("name", code.upper()),
                "balance": 0.0,
                "status": "missing",
                "enabled": False,
                "live": False,
                "source": "none",
            })
            continue
        status = acc.status.value if hasattr(acc.status, "value") else str(acc.status)
        bal = info.get("balance")
        if bal is None:
            bal = float(acc.balance or 0)
        sites.append({
            "code": code,
            "name": acc.name or meta.get("name", code.upper()),
            "balance": round(float(bal or 0), 2),
            "status": status,
            "enabled": bool(acc.enabled),
            "live": bool(info.get("live")),
            "source": info.get("source") or "cache",
            "last_sync_at": acc.last_sync_at.isoformat() if acc.last_sync_at else None,
        })
    return sites
