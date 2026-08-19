"""OB / 开云滚球场馆恢复：对标平博 recover — 已在盘口则不导航。"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def page_in_ob_venue(page) -> bool:
    """是否已在 OB/开云 H5 或体育场馆（可直接拉 matchesPB）。"""
    if page is None:
        return False
    try:
        from app.services.bookmakers.venue_entry import (
            is_in_sportsbook,
            page_already_on_live_board,
        )

        if await page_already_on_live_board(page) or await is_in_sportsbook(page):
            return True
    except Exception:
        pass
    try:
        from app.services.bookmakers.plugins.ob.odds import _collect_page_urls, _sport_ctx_from_url

        for u in await _collect_page_urls(page):
            _hu, st, _cuid = _sport_ctx_from_url(u)
            if st:
                return True
    except Exception:
        pass
    return False


async def recover_ob_live_venue(
    page,
    *,
    base_url: str = "",
    venue_url: str = "",
    session_token: str = "",
) -> bool:
    """
    空盘/掉厅时恢复滚球场馆。
    - 已在场馆：不 goto/reload，返回 True（调用方就地再拉）
    - 有 venue_url：恢复一次 H5
    - 否则软进馆（/game/sport → 点开云）
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

    if await page_in_ob_venue(page):
        logger.info("ob recover skip (already in venue) url=%s", cur)
        return True

    from app.services.bookmakers.plugins.ob.odds import (
        _collect_page_urls,
        _sport_ctx_from_url,
        sanitize_token,
    )

    base = (base_url or "").rstrip("/")
    if not base:
        try:
            from urllib.parse import urlparse

            p = urlparse(page.url or "")
            if p.netloc:
                base = f"{p.scheme}://{p.netloc}"
        except Exception:
            base = ""

    want = (venue_url or "").strip()
    if not want:
        try:
            from app.services.bookmakers.site_session import site_sessions

            sess = site_sessions.get(base) if base else None
            if sess:
                want = str(sess.venue_url or "").strip()
        except Exception:
            want = ""

    token = sanitize_token(session_token)

    # 1) 恢复验证时保存的 H5 venue_url
    if want:
        hu, st, _cuid = _sport_ctx_from_url(want)
        if st:
            try:
                from app.services.bookmakers.browser_login import (
                    apply_desktop_viewport,
                    dismiss_h5_orient_tip,
                )

                logger.info("ob recover: restore venue_url")
                await page.goto(want, wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(1000)
                await apply_desktop_viewport(page)
                try:
                    await dismiss_h5_orient_tip(page)
                except Exception:
                    pass
                if await page_in_ob_venue(page):
                    logger.info("ob recover done via venue_url url=%s", (page.url or "")[:140])
                    return True
                # URL 含 token 即视为可拉 API
                if st:
                    return True
            except Exception as e:
                logger.warning("ob restore venue_url failed: %s", e)

    # 2) 软进馆
    if not base:
        logger.warning("ob recover: no base_url, cannot soft re-enter")
        return False
    try:
        from app.services.bookmakers.browser_login import apply_desktop_viewport

        logger.info("ob recover: soft re-enter from url=%s", cur)
        await apply_desktop_viewport(page)
        await page.goto(base + "/", wait_until="domcontentloaded", timeout=45000)
        if token:
            await page.evaluate(
                "(t) => { try { localStorage.setItem('X-API-TOKEN', t); } catch (e) {} }",
                token,
            )
        await page.goto(base + "/game/sport", wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(1200)
        await apply_desktop_viewport(page)
        for label in ("开云体育", "ONE体育", "熊猫体育", "OB体育"):
            try:
                loc = page.get_by_text(label, exact=True).first
                if await loc.count() == 0:
                    loc = page.get_by_text(label, exact=False).first
                if await loc.count() == 0:
                    continue
                await loc.click(timeout=3000)
                break
            except Exception:
                continue
        await page.wait_for_timeout(1500)
        # 关闭「您当前操作将会离开游戏，是否继续？」弹窗（点确定继续进馆）
        try:
            from app.services.bookmakers.venue_entry import dismiss_blocking_modals

            await dismiss_blocking_modals(page)
            await page.wait_for_timeout(1000)
        except Exception:
            pass
        await page.wait_for_timeout(1000)
        for u in await _collect_page_urls(page):
            hu, st, _cuid = _sport_ctx_from_url(u)
            if st:
                try:
                    from app.services.bookmakers.site_session import site_sessions

                    sess = site_sessions.get(base)
                    if sess:
                        sess.venue_url = hu
                except Exception:
                    pass
                logger.info("ob recover soft re-enter ok url=%s", hu[:140])
                return True
        ok = await page_in_ob_venue(page)
        logger.info("ob recover soft re-enter done ok=%s url=%s", ok, (page.url or "")[:140])
        return ok
    except Exception as e:
        logger.warning("ob soft re-enter failed: %s", e)
        return False
