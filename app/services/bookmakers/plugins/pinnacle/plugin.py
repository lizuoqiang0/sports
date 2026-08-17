"""平博插件实现：滚球 URL / 正文刮取 / 空盘恢复；下单走通用 site_bet（UI）。"""
from __future__ import annotations

from typing import Any, Optional

from app.services.bookmakers.plugin import BasePlugin
from app.services.bookmakers.plugins.pinnacle.profile import PROFILE


class PinnaclePlugin(BasePlugin):
    def __init__(self) -> None:
        super().__init__(code="pinnacle", profile=dict(PROFILE))

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
        """优先 sports-service compact/events（足球+篮球）；空则 None 走 DOM/XHR 兜底。"""
        _ = session_token, venue_url
        from app.services.bookmakers.plugins.pinnacle.odds import fetch_pinnacle_live_odds

        rows = await fetch_pinnacle_live_odds(
            page,
            base_url=base_url or "",
            limit=limit,
            live_only=bool(live_only),
        )
        return list(rows) if rows else None

    async def after_empty_odds(self, page: Any) -> bool:
        """空盘先修复白屏，随后回到滚球列表并重试。"""
        from app.services.bookmakers.plugins.pinnacle.venue import (
            recover_pinnacle_blank_page,
            recover_pinnacle_live_list,
        )

        try:
            if not await recover_pinnacle_blank_page(page):
                return False
            url = ""
            try:
                url = (page.url or "").lower()
            except Exception:
                url = ""
            if "/live" in url:
                return True
            try:
                from app.services.bookmakers.venue_entry import page_already_on_live_board

                if await page_already_on_live_board(page):
                    return True
            except Exception:
                pass
            return bool(await recover_pinnacle_live_list(page))
        except Exception:
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
        if not (live_only and on_live_url):
            return rows
        from app.services.bookmakers.plugins.pinnacle.live_text import scrape_pinnacle_live_text

        try:
            extra = await scrape_pinnacle_live_text(
                page, url_sport=url_sport, limit=limit
            )
        except Exception:
            extra = []
        if not extra:
            return rows
        import logging

        log = logging.getLogger(__name__)
        placeholders = {"足球滚球", "篮球滚球", "滚球", "足球", "篮球", "", "未知联赛", "未知"}

        def _key(r: dict) -> str:
            return f"{(r.get('sport_hint') or '')}|{(r.get('home') or '').strip()}|{(r.get('away') or '').strip()}"

        def _league_rank(lg: str) -> int:
            s = (lg or "").strip()
            if not s or s in placeholders:
                return 0
            if any(k in s for k in ("联赛", "杯", "League", "Cup", "锦标赛")):
                return 3
            return 2

        by_key: dict[str, dict] = {}
        for r in list(rows or []):
            if not isinstance(r, dict):
                continue
            k = _key(r)
            if not k.endswith("||") and r.get("home") and r.get("away"):
                by_key[k] = dict(r)

        merged_n = 0
        added_n = 0
        for r in extra:
            if not isinstance(r, dict):
                continue
            k = _key(r)
            if not r.get("home") or not r.get("away"):
                continue
            prev = by_key.get(k)
            if not prev:
                by_key[k] = dict(r)
                added_n += 1
                continue
            merged_n += 1
            # 正文刮取通常比 DOM 更准：补联赛 / 比分 / 节次 / 时钟
            if _league_rank(str(r.get("league") or "")) > _league_rank(str(prev.get("league") or "")):
                prev["league"] = r.get("league")
            for fld in ("home_score", "away_score", "period", "clock", "live", "sport_hint", "raw"):
                if r.get(fld) not in (None, "", []):
                    prev[fld] = r.get(fld)
            if len(r.get("odds") or []) >= len(prev.get("odds") or []):
                prev["odds"] = r.get("odds")
            if r.get("under"):
                prev["under"] = r.get("under")
                prev["total_line"] = r.get("total_line")
            by_key[k] = prev

        log.info(
            "pinnacle text enrich: +%d new merge=%d total=%d url_sport=%s",
            added_n,
            merged_n,
            len(by_key),
            url_sport,
        )
        return list(by_key.values())[: max(limit * 2, 80)]

    def live_sport_urls(self, page_url: str = "", *, origin: str = "") -> list[str]:
        from app.services.bookmakers.plugins.pinnacle.venue import pinnacle_live_sport_urls

        return pinnacle_live_sport_urls(page_url, origin=origin)


PLUGIN = PinnaclePlugin()
