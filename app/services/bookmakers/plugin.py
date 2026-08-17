"""博彩站点插件协议：OB / 平博 分模块，共享层只做编排。"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional, Protocol, runtime_checkable


@runtime_checkable
class BookmakerPlugin(Protocol):
    code: str
    profile: dict[str, Any]

    async def fetch_live_odds(
        self,
        page: Any,
        *,
        base_url: str,
        session_token: str,
        limit: int,
        live_only: bool,
        venue_url: str = "",
    ) -> Optional[list]:
        """站点专属盘口拉取；返回 None 表示走通用 page scrape。"""
        ...

    async def after_empty_odds(self, page: Any) -> bool:
        """空结果时站点恢复动作；返回是否尝试了恢复。"""
        ...

    async def enrich_dom_rows(
        self,
        page: Any,
        rows: list,
        *,
        url_sport: str = "",
        live_only: bool = True,
        on_live_url: bool = True,
        limit: int = 80,
    ) -> list:
        """DOM 行补充（如平博正文刮取）。"""
        ...

    def live_sport_urls(self, page_url: str = "", *, origin: str = "") -> list[str]:
        ...

    async def place_bet(
        self,
        page: Any,
        *,
        base_url: str,
        session_token: str,
        match_external_id: str,
        selection: str,
        odds: float,
        stake: Decimal,
        bet_type: str,
        odds_data: dict,
    ) -> Any:
        """返回 PlaceBetResult；None 表示走通用 place_site_bet。"""
        ...


@dataclass
class BasePlugin:
    code: str
    profile: dict[str, Any] = field(default_factory=dict)

    async def fetch_live_odds(
        self,
        page: Any,
        *,
        base_url: str,
        session_token: str,
        limit: int,
        live_only: bool,
        venue_url: str = "",
    ) -> Optional[list]:
        return None

    async def after_empty_odds(self, page: Any) -> bool:
        return False

    async def enrich_dom_rows(
        self,
        page: Any,
        rows: list,
        *,
        url_sport: str = "",
        live_only: bool = True,
        on_live_url: bool = True,
        limit: int = 80,
    ) -> list:
        return rows

    def live_sport_urls(self, page_url: str = "", *, origin: str = "") -> list[str]:
        return []

    async def place_bet(
        self,
        page: Any,
        *,
        base_url: str,
        session_token: str,
        match_external_id: str,
        selection: str,
        odds: float,
        stake: Decimal,
        bet_type: str,
        odds_data: dict,
    ) -> Any:
        return None


_REGISTRY: dict[str, BookmakerPlugin] = {}
_LOADED = False


def register_plugin(plugin: BookmakerPlugin) -> None:
    _REGISTRY[(plugin.code or "").lower()] = plugin


def _ensure_plugins() -> None:
    global _LOADED
    if _LOADED:
        return
    # 延迟导入避免环
    from app.services.bookmakers.plugins.ob import plugin as ob_mod
    from app.services.bookmakers.plugins.pinnacle import plugin as pin_mod

    register_plugin(ob_mod.PLUGIN)
    register_plugin(pin_mod.PLUGIN)
    _LOADED = True


def get_plugin(code: str) -> BookmakerPlugin:
    _ensure_plugins()
    c = (code or "").lower()
    if c not in _REGISTRY:
        raise ValueError(f"未知站点插件: {c}（仅支持 {', '.join(sorted(_REGISTRY))}）")
    return _REGISTRY[c]


def list_plugins() -> list[BookmakerPlugin]:
    _ensure_plugins()
    return [_REGISTRY[k] for k in sorted(_REGISTRY)]
