"""博彩站点目录常量 — 从 plugins 聚合。"""
from __future__ import annotations

from typing import Any

from app.services.bookmakers.plugin import list_plugins


def _build_catalog() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for p in list_plugins():
        prof = p.profile
        out[p.code] = {
            "code": p.code,
            "name": prof.get("name") or p.code,
            "default_url": prof.get("default_url") or "",
            "default_balance": float(prof.get("default_balance") or 0.0),
        }
    return out


BOOKMAKER_CATALOG = _build_catalog()


def provider_name(code: str) -> str:
    return BOOKMAKER_CATALOG.get(code, {}).get("name", code)
