"""平博双球类：当 compact API 只返回足球时，补充获取篮球滚球数据。"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.services.bookmakers.base import RemoteMatch

logger = logging.getLogger(__name__)


async def scrape_other_live_sport(main_page: Any, *, limit: int = 80) -> list[RemoteMatch]:
    """补充获取另一球类的滚球数据（篮球）。

    策略：
    1. 用 guest HTTP 调 compact API sp=4（篮球），不导航
    2. 失败时导航到篮球滚球页，文本刮取
    """
    from app.services.bookmakers.plugins.pinnacle.odds import (
        _fetch_via_guest_http,
        parse_compact_events,
    )

    # 1. 尝试 guest HTTP 获取篮球 compact API
    try:
        origins = []
        try:
            page_url = main_page.url or ""
            if "://" in page_url:
                from urllib.parse import urlparse
                p = urlparse(page_url)
                origins = [f"{p.scheme}://{p.netloc}"]
        except Exception:
            origins = ["https://www.rowilong.com"]

        if not origins:
            origins = ["https://www.rowilong.com"]

        # 只请求篮球 sp=4
        payloads = []
        for origin in origins:
            try:
                from app.services.bookmakers.plugins.pinnacle.odds import _compact_events_url
                import httpx
                url = _compact_events_url(origin, sport_id=4, live_only=True)
                async with httpx.AsyncClient(timeout=15) as c:
                    r = await c.get(url, headers={
                        "User-Agent": "Mozilla/5.0",
                        "Accept": "application/json",
                    })
                    if r.status_code == 200:
                        data = r.json()
                        if data:
                            payloads.append(data)
                            logger.info("pinnacle dual-live guest http sp=4: 200 n=%d", len(payloads))
                            break
                    else:
                        logger.info("pinnacle dual-live guest http sp=4: status=%d", r.status_code)
            except Exception as e:
                logger.debug("pinnacle dual-live guest http sp=4 failed: %s", e)

        if payloads:
            # 调试：查看 sp=4 响应结构
            for i, p in enumerate(payloads):
                if isinstance(p, dict):
                    keys = list(p.keys())[:10]
                    l_data = p.get("l")
                    n_sports = len(l_data) if isinstance(l_data, list) else 0
                    logger.info("pinnacle dual-live sp=4 payload[%d] keys=%s n_sport_blocks=%d", i, keys, n_sports)
                    # 详细查看每个 sport_block
                    if isinstance(l_data, list):
                        for j, sb in enumerate(l_data):
                            if isinstance(sb, list):
                                sid = sb[0] if len(sb) > 0 else "?"
                                n_leagues = len(sb[2]) if len(sb) > 2 and isinstance(sb[2], list) else 0
                                logger.info("pinnacle dual-live sp=4 sport_block[%d] sport_id=%s n_leagues=%d", j, sid, n_leagues)
                                # 查看第一个联赛的事件结构
                                if isinstance(sb[2], list):
                                    for k, lg in enumerate(sb[2][:2]):
                                        if isinstance(lg, list) and len(lg) >= 3:
                                            lg_name = str(lg[1] or "")[:30]
                                            n_ev = len(lg[2]) if isinstance(lg[2], list) else 0
                                            logger.info("pinnacle dual-live sp=4 league[%d] name=%s n_events=%d", k, lg_name, n_ev)
                                            # 查看第一个事件的结构
                                            if isinstance(lg[2], list) and lg[2]:
                                                ev0 = lg[2][0]
                                                if isinstance(ev0, list):
                                                    logger.info("pinnacle dual-live sp=4 event[0] len=%d sample=%s", len(ev0), str(ev0[:5])[:200])
                                                else:
                                                    logger.info("pinnacle dual-live sp=4 event[0] type=%s val=%s", type(ev0).__name__, str(ev0)[:200])
                else:
                    logger.info("pinnacle dual-live sp=4 payload[%d] type=%s", i, type(p).__name__)
            rows = parse_compact_events(payloads, limit=limit, live_only=True)
            basketball = [m for m in rows if str(m.sport).lower() in ("basketball",)]
            logger.info("pinnacle dual-live sp=4 parsed=%d basketball=%d", len(rows), len(basketball))
            if basketball:
                logger.info("pinnacle dual-live: basketball=%d via guest http", len(basketball))
                return basketball
    except Exception as e:
        logger.warning("pinnacle dual-live guest http failed: %s", e)

    # 2. 导航到篮球滚球页，文本刮取
    original_url = ""
    try:
        original_url = main_page.url or ""
    except Exception:
        pass

    basketball_url = ""
    try:
        from app.services.bookmakers.plugins.pinnacle.plugin import PinnaclePlugin
        urls = PinnaclePlugin().live_sport_urls(original_url)
        for u in urls or []:
            if "basketball" in u.lower() and "/live" in u.lower():
                basketball_url = u
                break
        if not basketball_url:
            basketball_url = "https://www.rowilong.com/zh-cn/compact/sports/basketball/live"
    except Exception:
        basketball_url = "https://www.rowilong.com/zh-cn/compact/sports/basketball/live"

    try:
        logger.info("pinnacle dual-live: navigate to basketball %s", basketball_url[:80])
        await main_page.goto(basketball_url, wait_until="domcontentloaded", timeout=20000)
        await main_page.wait_for_timeout(2000)

        from app.services.bookmakers.plugins.pinnacle.live_text import scrape_pinnacle_live_text
        rows = await scrape_pinnacle_live_text(main_page, limit=limit)
        basketball = [m for m in rows if str(m.sport).lower() in ("basketball",)]
        if basketball:
            logger.info("pinnacle dual-live: basketball=%d via text scrape", len(basketball))
            return basketball
    except Exception as e:
        logger.warning("pinnacle dual-live text scrape failed: %s", e)
    finally:
        # 导航回原页面
        if original_url:
            try:
                await main_page.goto(original_url, wait_until="domcontentloaded", timeout=20000)
                await main_page.wait_for_timeout(1000)
            except Exception:
                pass

    return []
