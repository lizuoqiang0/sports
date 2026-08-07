"""平博滚球入口 / 列表恢复。"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

def pinnacle_live_sport_urls(page_url: str = "", *, origin: str = "") -> list[str]:
    """平博足球/篮球滚球直达 URL（优先 /live，避免早盘 sports 列表）。"""
    from urllib.parse import urlparse

    base = (origin or "").rstrip("/")
    if not base:
        raw = page_url or ""
        parsed = urlparse(raw if "://" in raw else f"https://{raw}")
        if parsed.netloc:
            base = f"{parsed.scheme}://{parsed.netloc}"
    if not base:
        return []
    return [
        f"{base}/zh-cn/compact/sports/soccer/live",
        f"{base}/zh-cn/compact/sports/basketball/live",
        f"{base}/en/compact/sports/soccer/live",
        f"{base}/en/compact/sports/basketball/live",
    ]



async def recover_pinnacle_live_list(page) -> bool:
    """
    平博：回到滚球列表便于同步采盘。
    已在 /live 滚球盘时绝不 goto/reload（SPA 刷新易白屏）；只在偏离盘口时恢复一次。
    """
    try:
        if page is None or page.is_closed():
            return False
    except Exception:
        return False

    try:
        cur = (page.url or "")[:160]
    except Exception:
        cur = ""
    # 已在滚球：视为成功，留给调用方就地刮盘（比分由站点自己推送更新）
    try:
        from app.services.bookmakers.venue_entry import page_already_on_live_board

        if await page_already_on_live_board(page) or "/live" in (cur or "").lower():
            logger.info("pinnacle recover skip (already on live) url=%s", cur)
            return True
    except Exception:
        if "/live" in (cur or "").lower():
            logger.info("pinnacle recover skip (url /live) url=%s", cur)
            return True
    logger.info("pinnacle recover live list from url=%s", cur)

    # 1) 直达滚球 URL（早盘 /sports/soccer 点「滚球」常无效）
    try:
        from urllib.parse import urlparse

        raw = page.url or ""
        parsed = urlparse(raw if "://" in raw else f"https://{raw}")
        origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else ""
        if not origin:
            return False
        opened = False
        for dest in pinnacle_live_sport_urls(origin=origin)[:2]:
            try:
                await page.goto(dest, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(1100)
                opened = True
                break
            except Exception:
                continue
        if opened:
            for text in ("滚球盘", "滚球", "In-Play", "Live"):
                try:
                    loc = page.get_by_text(text, exact=False).first
                    if await loc.count() > 0:
                        await loc.click(timeout=2000)
                        await page.wait_for_timeout(700)
                        break
                except Exception:
                    continue
            from app.services.bookmakers.venue_entry import page_is_off_match_list
            ok = not await page_is_off_match_list(page)
            try:
                logger.info(
                    "pinnacle recover done ok=%s url=%s",
                    ok,
                    (page.url or "")[:140],
                )
            except Exception:
                pass
            if ok:
                return True
    except Exception as e:
        logger.warning("pinnacle recover goto live failed: %s", e)

    # 2) 点侧栏「滚球盘 / In-Play」兜底
    for text in ("滚球盘", "In-Play", "In Play", "Live Betting"):
        try:
            loc = page.get_by_text(text, exact=False).first
            if await loc.count() > 0:
                await loc.click(timeout=2500)
                await page.wait_for_timeout(900)
                from app.services.bookmakers.venue_entry import page_already_on_live_board, page_is_off_match_list
                if not await page_is_off_match_list(page):
                    if await page_already_on_live_board(page) or "/live" in (
                        (page.url or "").lower()
                    ):
                        return True
        except Exception:
            continue
    return False


