"""平博滚球入口 / 列表恢复。"""
from __future__ import annotations

import logging
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


async def pinnacle_session_expired(page) -> bool:
    """判断平博是否已回到登录态，避免把失效 cookie 当作有效长连接。"""
    try:
        if page is None or page.is_closed():
            return False
        page_url = (page.url or "").lower()
    except Exception:
        return False

    if any(marker in page_url for marker in ("/login", "/signin", "/sign-in", "/verify", "/captcha")):
        return True

    try:
        state = await page.evaluate(
            r"""() => {
              const visible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== 'none' && style.visibility !== 'hidden'
                  && Number(style.opacity || 1) > 0 && rect.width > 1 && rect.height > 1;
              };
              const password = [...document.querySelectorAll(
                'input[type="password"], input[name*="password" i], input[autocomplete="current-password"]'
              )].some(visible);
              const username = [...document.querySelectorAll(
                'input[type="text"], input[type="email"], input[type="tel"], input:not([type])'
              )].some((el) => {
                if (!visible(el)) return false;
                const hint = (el.name || '') + ' ' + (el.id || '') + ' ' + (el.placeholder || '');
                return !/搜索|search|验证码|verify|captcha|code/i.test(hint);
              });
              const body = String((document.body && document.body.innerText) || '');
              const guest = /访客用户看到的赔率存在延迟|请登录或注册以查看|guest.+odds.+delay|log\s*in\s*or\s*register/i.test(body);
              const loginAction = [...document.querySelectorAll('button, a, [role="button"]')]
                .some((el) => visible(el) && /^(登录|登入|log\s*in|sign\s*in)$/i.test(
                  String(el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim()
                ));
              // 盘口页也可能带有非登录密码控件；必须同时存在可见账号和密码输入才算退出。
              return {password, username, guest, loginAction, loginSurface: (password && username) || guest};
            }"""
        )
    except Exception:
        return False
    return bool(isinstance(state, dict) and state.get("loginSurface"))


def pinnacle_live_sport_urls(page_url: str = "", *, origin: str = "") -> list[str]:
    """平博足球/篮球滚球直达 URL（优先 /live，避免早盘 sports 列表）。"""
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


async def pinnacle_page_is_blank(page) -> bool:
    """判断平博 SPA 是否落入白屏状态，避免把空页面当作无滚球。"""
    try:
        if page is None or page.is_closed():
            return True
        page_url = (page.url or "").lower()
        # 登录/验证页的初始 DOM 可能短暂为空，不能在认证流程中反复刷新。
        if any(marker in page_url for marker in ("/login", "/signin", "/verify", "/captcha")):
            return False
        state = await page.evaluate(
            """() => {
                const body = document.body;
                if (!body) return {ready: document.readyState, textLength: 0, hasVisibleControl: false};
                const hasVisibleControl = [...document.querySelectorAll(
                    'a,button,input,select,textarea,[role="button"],[data-testid],[data-test-id]'
                )].some((el) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    return style.display !== 'none' && style.visibility !== 'hidden'
                        && Number(style.opacity || 1) > 0 && rect.width > 1 && rect.height > 1;
                });
                return {
                    ready: document.readyState,
                    textLength: (body.innerText || '').trim().length,
                    hasVisibleControl,
                };
            }"""
        )
    except Exception:
        return True
    if not isinstance(state, dict) or state.get("ready") != "complete":
        return False
    return int(state.get("textLength") or 0) < 12 and not bool(
        state.get("hasVisibleControl")
    )


async def recover_pinnacle_blank_page(page, *, attempts: int = 3, venue_url: str = "") -> bool:
    """白屏时重载并复检；reload 失败则 goto 直达场馆 URL 兜底。"""
    if not await pinnacle_page_is_blank(page):
        return True

    total_attempts = max(1, int(attempts))
    for attempt in range(1, total_attempts + 1):
        try:
            logger.warning(
                "pinnacle blank page detected, reload attempt=%s/%s url=%s",
                attempt,
                total_attempts,
                (page.url or "")[:120],
            )
            await page.reload(wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(2500)
        except Exception as e:
            logger.warning("pinnacle blank page reload failed attempt=%s: %s", attempt, e)
            # reload 失败（ERR_ABORTED/frame detached）→ goto 兜底
            if venue_url:
                try:
                    logger.info("pinnacle blank page goto fallback url=%s", venue_url[:120])
                    await page.goto(venue_url, wait_until="domcontentloaded", timeout=45000)
                    await page.wait_for_timeout(3000)
                except Exception as e2:
                    logger.warning("pinnacle blank page goto also failed: %s", e2)
        if not await pinnacle_page_is_blank(page):
            logger.info("pinnacle blank page recovered attempt=%s", attempt)
            return True

    logger.error("pinnacle blank page persists after %s reloads", total_attempts)
    return False


async def page_shows_maintenance(page) -> bool:
    """平博页面是否被「维护」横幅遮罩（多为过期 DOM 遮罩，站点实际未维护）。"""
    try:
        if page is None or page.is_closed():
            return False
    except Exception:
        return False
    try:
        t = await page.evaluate(
            "() => ((document.body && document.body.innerText) || '')"
        )
    except Exception:
        return False
    t = t or ""
    return ("正在维护" in t) or ("维护中" in t) or ("系统维护" in t)


async def clear_pinnacle_maintenance(page) -> bool:
    """维护横幅自动恢复：整页刷新即清除（实测刷新页面就恢复）。

    返回是否执行了恢复动作（检测到横幅）。刷新失败退回 goto 直达滚球
    URL（全新 SPA 加载同样能清遮罩）。
    """
    if not await page_shows_maintenance(page):
        return False
    logger.warning("pinnacle maintenance banner detected, auto refresh url=%s", (page.url or "")[:100])
    try:
        await page.reload(wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(3000)
    except Exception as e:
        logger.warning("pinnacle maintenance reload failed: %s, fallback goto", e)
        try:
            raw = page.url or ""
            pu = urlparse(raw if "://" in raw else f"https://{raw}")
            origin = f"{pu.scheme}://{pu.netloc}" if pu.netloc else ""
            if origin:
                for dest in pinnacle_live_sport_urls(origin=origin)[:2]:
                    try:
                        await page.goto(dest, wait_until="domcontentloaded", timeout=45000)
                        await page.wait_for_timeout(2500)
                        break
                    except Exception:
                        continue
        except Exception:
            pass
    still = await page_shows_maintenance(page)
    logger.warning(
        "pinnacle maintenance auto-refresh done still_banner=%s url=%s",
        still, (page.url or "")[:100],
    )
    return True



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
    # 就地原则：compact/sports/ 下任意页（soccer/basketball/live）都不再导航——
    # 采盘走 sports-service API（fetch compact/events），不依赖页面在 /live；
    # 频繁 goto 会打断用户手动浏览并易致 SPA 白屏
    cur_l = (cur or "").lower()
    if "/live" in cur_l or "/compact/sports/" in cur_l:
        logger.info("pinnacle recover skip (on sports pages) url=%s", cur)
        return True
    try:
        from app.services.bookmakers.venue_entry import page_already_on_live_board

        if await page_already_on_live_board(page):
            logger.info("pinnacle recover skip (already on live) url=%s", cur)
            return True
    except Exception:
        pass
    logger.info("pinnacle recover live list from url=%s", cur)

    # 只允许一次确定性的直达。不要再点击侧栏/球类/滚球 Tab；平博 SPA
    # 的这些点击会继续触发路由并把登录后的页面带到错误页。
    try:
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
            try:
                logger.info(
                    "pinnacle sports entry done url=%s",
                    (page.url or "")[:140],
                )
            except Exception:
                pass
            return True
    except Exception as e:
        logger.warning("pinnacle recover goto live failed: %s", e)
    return False
