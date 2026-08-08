from __future__ import annotations

import asyncio
import logging

import httpx

from app.config import settings
from app.core.crypto import decrypt_secret
from app.services.bookmakers.gate_client import _gate_headers

logger = logging.getLogger(__name__)


async def verify_ob_order_exists(
    site_acc,
    order_no: str,
    *,
    fail_open_on_exception: bool,
) -> bool:
    """验证 OB 返回的 orderNo 是否已真实落入注单历史。"""
    gate = (settings.BOOKMAKER_BROWSER_GATE_URL or "").rstrip("/")
    if not gate:
        return True

    order_no = str(order_no or "").strip()
    if not order_no:
        return False

    retries = max(1, int(getattr(settings, "OB_BET_VERIFY_RETRIES", 8) or 8))
    initial_delay = max(0.0, float(getattr(settings, "OB_BET_VERIFY_INITIAL_DELAY_SEC", 4.0) or 4.0))
    interval = max(0.0, float(getattr(settings, "OB_BET_VERIFY_INTERVAL_SEC", 2.0) or 2.0))
    history_days = max(1, int(getattr(settings, "OB_BET_VERIFY_HISTORY_DAYS", 1) or 1))

    had_exception = False
    saw_valid_response = False
    session_token = ""
    try:
        if getattr(site_acc, "session_token_encrypted", None):
            session_token = decrypt_secret(site_acc.session_token_encrypted)
    except Exception as e:
        logger.warning("OB 下单验证解密 session 失败 orderNo=%s: %s", order_no, e)
        if fail_open_on_exception:
            return True
        return False

    if initial_delay > 0:
        await asyncio.sleep(initial_delay)

    async with httpx.AsyncClient(timeout=45.0, headers=_gate_headers()) as client:
        for attempt in range(1, retries + 1):
            try:
                resp = await client.post(
                    f"{gate}/bets/history",
                    json={
                        "site_code": "ob",
                        "base_url": getattr(site_acc, "base_url", "") or "",
                        "session_token": session_token,
                        "days": history_days,
                    },
                )
                if 500 <= resp.status_code:
                    logger.warning(
                        "OB 下单验证响应异常: orderNo=%s attempt=%s/%s status=%s",
                        order_no,
                        attempt,
                        retries,
                        resp.status_code,
                    )
                else:
                    data = resp.json() if resp.content else {}
                    orders = data.get("orders") or []
                    saw_valid_response = True
                    for od in orders:
                        if str(od.get("external_bet_id") or "") == order_no:
                            logger.info(
                                "OB 下单验证通过: orderNo=%s attempt=%s/%s",
                                order_no,
                                attempt,
                                retries,
                            )
                            return True
                    logger.warning(
                        "OB 下单验证未命中: orderNo=%s attempt=%s/%s orders=%s",
                        order_no,
                        attempt,
                        retries,
                        len(orders),
                    )
            except Exception as e:
                had_exception = True
                logger.warning(
                    "OB 下单验证异常: orderNo=%s attempt=%s/%s err=%s",
                    order_no,
                    attempt,
                    retries,
                    e,
                )

            if attempt < retries and interval > 0:
                await asyncio.sleep(interval)

    if fail_open_on_exception and had_exception and not saw_valid_response:
        logger.warning(
            "OB 下单验证连续异常，按宽松模式放行: orderNo=%s retries=%s",
            order_no,
            retries,
        )
        return True

    logger.warning(
        "OB 下单验证最终失败: orderNo=%s retries=%s fail_open=%s",
        order_no,
        retries,
        fail_open_on_exception,
    )
    return False
