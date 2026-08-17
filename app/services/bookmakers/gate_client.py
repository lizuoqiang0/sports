"""
Browser Gate HTTP 客户端：URL / health / login / balance / odds-sync / bet。

连接器与 poller 共用，避免重复实现超时与 busy 判断。
"""
from __future__ import annotations

import logging
import os
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)


def gate_url() -> str:
    return (os.getenv("BOOKMAKER_BROWSER_GATE_URL") or "").rstrip("/")


def _gate_headers() -> dict[str, str]:
    token = (os.getenv("INTERNAL_API_TOKEN") or "").strip()
    if not token:
        return {}
    return {"X-Internal-Token": token}


def to_decimal(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    try:
        if value is None or value == "":
            return default
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return default


async def fetch_health(*, timeout: float = 3.0) -> dict:
    """GET /health；失败返回空 dict。"""
    gate = gate_url()
    if not gate:
        return {}
    try:
        async with httpx.AsyncClient(timeout=timeout, headers=_gate_headers()) as client:
            resp = await client.get(f"{gate}/health")
            if resp.status_code != 200:
                return {}
            data = resp.json()
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


async def is_gate_too_busy(*, min_busy: int = 2, timeout: float = 3.0) -> bool:
    """Gate 已有 ≥min_busy 条 lane 在忙时返回 True。"""
    data = await fetch_health(timeout=timeout)
    if not data:
        return False
    ops = data.get("busy_ops") or []
    return len(ops) >= min_busy


async def fetch_session_balances(*, timeout: float = 3.0) -> dict[str, float]:
    """从 health.session_balances 汇总各站余额（同站取较大值）。"""
    data = await fetch_health(timeout=timeout)
    out: dict[str, float] = {}
    for item in data.get("session_balances") or []:
        if not isinstance(item, dict):
            continue
        code = str(item.get("site_code") or "").strip().lower()
        try:
            bal = float(item.get("balance") or 0)
        except Exception:
            bal = 0.0
        if code and bal >= 0:
            prev = out.get(code)
            if prev is None or bal > prev:
                out[code] = bal
    return out


async def prefer_login(
    *,
    base_url: str,
    site_code: str,
    seconds: int = 45,
    timeout: float = 2.0,
) -> None:
    gate = gate_url()
    if not gate:
        return
    try:
        async with httpx.AsyncClient(timeout=timeout, headers=_gate_headers()) as client:
            await client.post(
                f"{gate}/prefer-login",
                json={"seconds": seconds, "base_url": base_url, "site_code": site_code},
            )
    except Exception:
        pass


async def post_login(
    *,
    base_url: str,
    username: str,
    password: str,
    session_token: str = "",
    site_code: str,
    wait_seconds: int = 35,
    force_new: bool = False,
    manual_venue: bool = False,
    timeout: float = 150.0,
) -> dict:
    """POST /login；网络失败返回 {ok:False, message:...}。"""
    gate = gate_url()
    if not gate:
        return {"ok": False, "message": "未配置 BOOKMAKER_BROWSER_GATE_URL"}
    try:
        async with httpx.AsyncClient(timeout=timeout, headers=_gate_headers()) as client:
            resp = await client.post(
                f"{gate}/login",
                json={
                    "base_url": base_url,
                    "username": username,
                    "password": password,
                    "session_token": session_token or "",
                    "wait_seconds": wait_seconds,
                    "force_new": force_new,
                    "site_code": site_code,
                    "manual_venue": manual_venue,
                },
            )
            data = resp.json()
            return data if isinstance(data, dict) else {"ok": False, "message": "网关返回异常"}
    except Exception as e:
        err = str(e).strip() or type(e).__name__
        logger.warning("browser gate login failed (%s): %s", site_code, err)
        return {"ok": False, "message": f"调用浏览器网关失败: {err}"}


async def post_balance(
    *,
    base_url: str,
    session_token: str,
    site_code: str,
    timeout: float = 20.0,
) -> dict:
    gate = gate_url()
    if not gate:
        return {}
    try:
        async with httpx.AsyncClient(timeout=timeout, headers=_gate_headers()) as client:
            resp = await client.post(
                f"{gate}/balance",
                json={
                    "base_url": base_url,
                    "session_token": session_token or "",
                    "site_code": site_code,
                },
            )
            if resp.status_code != 200:
                return {}
            data = resp.json()
            return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.debug("gate balance failed (%s): %s", site_code, e)
        return {}


async def post_odds_sync(
    *,
    base_url: str,
    session_token: str,
    site_code: str,
    live_only: bool = True,
    limit: int = 800,
    venue_url: str = "",
    headed: bool = False,
    timeout: Optional[float] = None,
) -> Optional[dict]:
    """
    POST /odds/sync-live 或 /odds/sync。
    成功返回完整 JSON；网络异常返回 None；busy/失败也返回 dict（含 ok/busy）。
    """
    gate = gate_url()
    if not gate or not session_token:
        return None
    path = "/odds/sync-live" if live_only else "/odds/sync"
    if timeout is None:
        timeout = 75.0 if live_only else 180.0
    try:
        async with httpx.AsyncClient(timeout=timeout, headers=_gate_headers()) as client:
            resp = await client.post(
                f"{gate}{path}",
                json={
                    "base_url": base_url,
                    "session_token": session_token,
                    "limit": limit,
                    "headed": headed,
                    "live_only": live_only,
                    "site_code": site_code,
                    "venue_url": venue_url or "",
                },
            )
            data = resp.json()
            return data if isinstance(data, dict) else None
    except Exception as e:
        logger.warning("gate odds %s failed: %s", site_code, e)
        return None


async def post_place_bet(
    *,
    base_url: str,
    session_token: str,
    site_code: str,
    match_external_id: str,
    selection: str,
    odds: float,
    stake: float,
    bet_type: str = "total",
    odds_data: Optional[dict] = None,
    headed: bool = False,
    timeout: float = 90.0,
) -> dict:
    gate = gate_url()
    if not gate:
        return {"ok": False, "message": "未配置浏览器网关"}
    try:
        async with httpx.AsyncClient(timeout=timeout, headers=_gate_headers()) as client:
            resp = await client.post(
                f"{gate}/bet/place",
                json={
                    "base_url": base_url,
                    "session_token": session_token,
                    "match_external_id": match_external_id,
                    "selection": selection,
                    "odds": float(odds),
                    "stake": float(stake),
                    "bet_type": bet_type,
                    "odds_data": odds_data or {},
                    "headed": headed,
                    "site_code": site_code,
                },
            )
            data = resp.json()
            return data if isinstance(data, dict) else {"ok": False, "message": "网关返回异常"}
    except Exception as e:
        logger.warning("gate place_bet %s failed: %s", site_code, e)
        return {"ok": False, "message": f"浏览器网关下单失败: {e}"}


# 兼容旧私有名（bookmakers API / live_poller 等仍可能引用）
_gate_url = gate_url
_to_decimal = to_decimal
