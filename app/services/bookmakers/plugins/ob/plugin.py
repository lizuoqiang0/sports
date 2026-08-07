"""OB 插件实现：YBTY API 盘口 + betOrder 下单；空盘恢复对标平博。"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Optional

from app.services.bookmakers.plugin import BasePlugin
from app.services.bookmakers.plugins.ob.profile import PROFILE

logger = logging.getLogger(__name__)


class ObPlugin(BasePlugin):
    def __init__(self) -> None:
        super().__init__(code="ob", profile=dict(PROFILE))

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
        from app.services.bookmakers.plugins.ob.odds import fetch_ob_live_odds

        vu = (venue_url or "").strip()
        if not vu and page is not None:
            try:
                from app.services.bookmakers.site_session import site_sessions

                s = site_sessions.get(base_url)
                if s:
                    vu = str(s.venue_url or "").strip()
            except Exception:
                vu = ""
        rows = await fetch_ob_live_odds(
            base_url=base_url,
            session_token=session_token,
            limit=limit,
            headed=False,
            live_only=bool(live_only),
            page=page,
            refresh_first=False,
            venue_url=vu,
        )
        # 有结果则返回；空则 None，让 gate 走通用 page scrape / after_empty
        return list(rows) if rows else None

    async def after_empty_odds(self, page: Any) -> bool:
        """已在场馆：不导航，返回 True 触发就地再拉；偏离时 recover 一次。"""
        from app.services.bookmakers.plugins.ob.venue import (
            page_in_ob_venue,
            recover_ob_live_venue,
        )

        try:
            if await page_in_ob_venue(page):
                logger.info("ob after_empty: already in venue — re-fetch in place")
                return True
            base = ""
            venue_url = ""
            token = ""
            try:
                from urllib.parse import urlparse

                p = urlparse(page.url or "")
                if p.netloc:
                    base = f"{p.scheme}://{p.netloc}"
            except Exception:
                pass
            try:
                from app.services.bookmakers.site_session import site_sessions

                sess = site_sessions.get(base) if base else None
                if sess:
                    venue_url = str(sess.venue_url or "").strip()
                    token = str(getattr(sess, "token", "") or "")
                    if not base:
                        base = str(getattr(sess, "base_url", "") or "")
            except Exception:
                pass
            return bool(
                await recover_ob_live_venue(
                    page,
                    base_url=base,
                    venue_url=venue_url,
                    session_token=token,
                )
            )
        except Exception as e:
            logger.warning("ob after_empty_odds failed: %s", e)
            return False

    def live_sport_urls(self, page_url: str = "", *, origin: str = "") -> list[str]:
        """OB 无 compact /live 直达；优先返回会话保存的 venue_url。"""
        _ = page_url
        try:
            from app.services.bookmakers.site_session import site_sessions

            base = (origin or "").rstrip("/")
            if not base and page_url:
                from urllib.parse import urlparse

                p = urlparse(page_url if "://" in page_url else f"https://{page_url}")
                if p.netloc:
                    base = f"{p.scheme}://{p.netloc}"
            sess = site_sessions.get(base) if base else None
            vu = str(getattr(sess, "venue_url", "") or "").strip() if sess else ""
            return [vu] if vu else []
        except Exception:
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
        from app.services.bookmakers.plugins.ob.bet import place_ybty_bet

        return await place_ybty_bet(
            base_url=base_url,
            session_token=session_token,
            match_external_id=match_external_id,
            selection=selection,
            odds=float(odds),
            stake=stake,
            bet_type=bet_type,
            odds_data=odds_data or {},
            headed=False,
            page=page,
            allow_launch=False,
        )


PLUGIN = ObPlugin()
