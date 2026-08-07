"""博彩站点连接器注册表 — 仅 OB / 平博。"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.services.bookmakers.catalog import BOOKMAKER_CATALOG
from app.services.bookmakers.plugins.ob.kaiyun import is_demo_url
from app.services.bookmakers.site_connector import BrowserSiteConnector, create_site_connector
from app.services.bookmakers.site_profiles import LIVE_SITE_CODES
from app.services.live_mode import ensure_live_connector_url


def get_connector(
    code: str,
    *,
    base_url: str,
    username: str,
    password: str,
    balance: Decimal | float | None = None,
    session_token: str | None = None,
    profile: dict | None = None,
    **kwargs: Any,
) -> BrowserSiteConnector:
    """返回统一 BrowserSiteConnector（OB / 平博）。"""
    code_l = (code or "").lower()
    if code_l in ("saba", "fb"):
        raise ValueError(f"站点已移除: {code_l}（仅支持 ob/pinnacle）")
    ensure_live_connector_url(code_l, base_url)
    if is_demo_url(base_url):
        raise ValueError(f"禁止演示站连接器: {code_l}（请填写真实网址）")

    if code_l in ("ob", "pinnacle"):
        return create_site_connector(
            code_l,
            base_url=base_url,
            username=username,
            password=password,
            balance=balance,
            session_token=session_token or "",
            profile=profile or {},
            **kwargs,
        )
    raise ValueError(f"未知站点代码: {code_l}（仅支持 ob/pinnacle）")


def list_catalog() -> list[dict]:
    return [
        {
            "code": c["code"],
            "name": c["name"],
            "default_url": c["default_url"],
        }
        for c in BOOKMAKER_CATALOG.values()
    ]


def is_real_live_account(code: str, base_url: str) -> bool:
    return (code or "").lower() in LIVE_SITE_CODES and not is_demo_url(base_url)
