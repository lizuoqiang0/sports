"""站点画像：从 plugins 聚合 OB + 平博。"""
from __future__ import annotations

import os
from typing import Any

from app.services.bookmakers.plugin import get_plugin, list_plugins


def _profiles() -> dict[str, dict[str, Any]]:
    return {p.code: dict(p.profile) for p in list_plugins()}


# 兼容旧导入：动态聚合
def __getattr__(name: str) -> Any:
    if name == "SITE_PROFILES":
        return _profiles()
    if name == "LIVE_SITE_CODES":
        return tuple(p.code for p in list_plugins())
    if name == "MANUAL_VENUE_SITE_CODES":
        return tuple(
            p.code
            for p in list_plugins()
            if p.profile.get("manual_venue") or p.profile.get("portal")
        )
    if name == "PORTAL_SITE_CODES":
        return tuple(p.code for p in list_plugins() if p.profile.get("portal"))
    raise AttributeError(name)


# 静态常量（启动时解析一次；reload_plugins 后请用函数）
LIVE_SITE_CODES = ("ob", "pinnacle")
MANUAL_VENUE_SITE_CODES: tuple[str, ...] = ("ob",)
PORTAL_SITE_CODES = ("ob",)


def disabled_site_codes() -> frozenset[str]:
    """临时关闭站点登录/轮询：BOOKMAKER_DISABLE_SITES=ob 或 ob,pinnacle。"""
    raw = (os.getenv("BOOKMAKER_DISABLE_SITES") or "").strip().lower()
    if not raw:
        return frozenset()
    return frozenset(x.strip() for x in raw.replace(";", ",").split(",") if x.strip())


def is_site_disabled(code: str) -> bool:
    return (code or "").lower() in disabled_site_codes()


SITE_PROFILES: dict[str, dict[str, Any]] = _profiles()


def get_site_profile(code: str) -> dict[str, Any]:
    c = (code or "").lower()
    try:
        return dict(get_plugin(c).profile)
    except ValueError:
        return dict(get_plugin("ob").profile)


def is_live_site_code(code: str) -> bool:
    c = (code or "").lower()
    if is_site_disabled(c):
        return False
    return c in tuple(p.code for p in list_plugins())


def needs_manual_venue(code: str) -> bool:
    """Only portal/manual profiles require a human venue-entry step.

    Pinnacle has a stable compact sportsbook URL and must remain fully automatic;
    forcing it into manual mode previously let a guest page pass as connected.
    """
    profile = get_site_profile(code)
    return bool(profile.get("manual_venue") or profile.get("portal"))
