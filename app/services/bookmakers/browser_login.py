"""
可见浏览器登录：打开目标网址 -> 预填账号密码 -> 等待登录成功。

供 BrowserSiteConnector 与本机 browser_gate 共用。
始终弹出可见 Chromium（headless 已移除），验证成功后保持窗口可见长连接。
"""
from __future__ import annotations

import logging
import os
import platform
import re
import time
from decimal import Decimal
from typing import Any, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


def site_origin(base_url: str) -> str:
    """登录/首页导航只用 origin。账号 base_url 可能是盘口深链（…/sports/soccer）。"""
    raw = (base_url or "").strip()
    if not raw:
        return ""
    try:
        u = urlparse(raw if "://" in raw else f"https://{raw}")
        if u.scheme and u.netloc:
            return f"{u.scheme}://{u.netloc}".rstrip("/")
    except Exception:
        pass
    return raw.rstrip("/")

# 主站 / 登录 / 进场馆：桌面端（避免落到 APP 下载页）
DESKTOP_VIEWPORT = {"width": 1440, "height": 900}
DESKTOP_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
# 仅 H5 体育场馆页用竖屏视口，去掉「请将手机竖屏」遮罩（不换 UA，不影响主站）
H5_PORTRAIT_VIEWPORT = {"width": 390, "height": 844}


def is_linux_server() -> bool:
    return platform.system() == "Linux"


def use_headed_browser() -> bool:
    """始终弹出可见浏览器。"""
    return True


def _is_h5_venue_url(url: str) -> bool:
    u = (url or "").lower()
    return any(
        x in u
        for x in (
            "app-h5",
            "/#/match",
            "zlshelves",
            "yewu11",
            "h5.",
            "m-h5",
        )
    )


async def apply_desktop_viewport(page) -> None:
    """主站桌面视口。"""
    try:
        await page.set_viewport_size(DESKTOP_VIEWPORT)
    except Exception:
        pass


async def dismiss_h5_orient_tip(page) -> None:
    """
    仅针对 H5 场馆页：竖屏视口 + 去掉「请将手机竖屏」遮罩。
    主站必须保持桌面 UA/视口，否则会落到 APP 下载落地页，无法拉真实赛事。
    """
    try:
        url = page.url or ""
    except Exception:
        url = ""
    if url and not _is_h5_venue_url(url):
        return
    try:
        await page.set_viewport_size(H5_PORTRAIT_VIEWPORT)
    except Exception:
        pass
    try:
        await page.add_style_tag(
            content="""
            .orient-mask, .orientTip, .orientation-tip, .rotate-tip,
            [class*="orient"], [class*="Orient"], [class*="rotate-phone"] {
              display: none !important; visibility: hidden !important; pointer-events: none !important;
            }
            """
        )
    except Exception:
        pass
    try:
        await page.evaluate(
            """() => {
              const kill = () => {
                document.querySelectorAll('div,section').forEach((el) => {
                  const t = (el.innerText || '').trim();
                  if (t.includes('请将手机竖屏') || t.includes('竖屏操作')) {
                    el.style.setProperty('display', 'none', 'important');
                  }
                });
              };
              kill();
              setTimeout(kill, 500);
              setTimeout(kill, 1500);
            }"""
        )
    except Exception:
        pass


# 兼容旧名
async def apply_mobile_portrait(page) -> None:
    await dismiss_h5_orient_tip(page)


def _pick(data: dict, *keys: str, default=None):
    for k in keys:
        if isinstance(data, dict) and k in data and data[k] is not None:
            return data[k]
    return default


def _unwrap_payload(body: Any) -> dict:
    if not isinstance(body, dict):
        return {}
    data = body.get("data")
    if isinstance(data, dict):
        return data
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    return body


def _to_decimal(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    try:
        if value is None or value == "":
            return default
        return Decimal(str(value))
    except Exception:
        return default


def extract_profile(payload: dict, username: str = "") -> dict:
    """提取资料；余额仅取体育/场馆钱包，忽略中心钱包。"""
    data = _unwrap_payload(payload) if payload else {}
    name = _pick(data, "name", "userName", "username", "nickName", "memberName", default=username)
    member_id = _pick(data, "id", "memberId", "userId", "uid", default="")
    # 只认场馆/体育钱包字段
    # 只认明确体育/场馆字段；transfer/game 在综合站常是中心可转，勿当体育余额
    balance = _pick(
        data,
        "sportBalance",
        "venueBalance",
        "tyBalance",
        "ybtyBalance",
        "sportWalletBalance",
        "sportsBalance",
        default=None,
    )
    if balance is None:
        for nest_key in ("sportWallet", "venueWallet", "wallet", "memberWallet", "finance"):
            if nest_key in ("centerWallet", "gameWallet"):
                continue
            wallet = data.get(nest_key) or {}
            if isinstance(wallet, dict):
                balance = _pick(
                    wallet,
                    "sportBalance",
                    "venueBalance",
                    "tyBalance",
                    "ybtyBalance",
                    "balance",
                    "amount",
                    "money",
                    "availableBalance",
                    "usableBalance",
                    default=None,
                )
                if balance is not None:
                    break
    # 列表钱包：只取名称像体育/场馆的项
    if balance is None:
        for nest_key in ("wallets", "walletList", "accountList"):
            items = data.get(nest_key)
            if not isinstance(items, list):
                continue
            best = None
            for w in items:
                if not isinstance(w, dict):
                    continue
                label = str(
                    w.get("name") or w.get("walletName") or w.get("type") or w.get("walletType") or ""
                ).lower()
                if any(x in label for x in ("center", "中心", "main", "lobby", "大厅", "现金", "transfer", "可转")):
                    continue
                if not any(x in label for x in ("sport", "venue", "体育", "场馆", "ty", "ybty", "开云")):
                    continue
                v = _pick(w, "balance", "amount", "money", "availableBalance", "sportBalance", default=None)
                if v is None:
                    continue
                dv = _to_decimal(v)
                if best is None or dv > best:
                    best = dv
            if best is not None:
                balance = best
                break
    profile = {
        "name": str(name or username),
        "member_id": str(member_id) if member_id is not None else "",
    }
    if balance is not None:
        profile["balance"] = float(_to_decimal(balance))
        profile["balance_source"] = "venue"
    return profile


_NAV_ERR_MARKERS = (
    "execution context was destroyed",
    "cannot find context with specified id",
    "target closed",
    "frame was detached",
    "navigating",
)

PWD_SELECTOR = (
    'input[name="password"], input[type="password"], '
    'input[placeholder*="密码"], input[placeholder*="Password"], '
    'input[placeholder*="口令"], input[autocomplete="current-password"]'
)
USER_SELECTOR = (
    'input[name="name"], input[name="username"], input[name="account"], input[name="user"], '
    'input[name="loginName"], input[name="login_name"], input[name="member"], '
    'input[placeholder*="账号"], input[placeholder*="用户"], input[placeholder*="帐号"], '
    'input[placeholder*="手机"], input[placeholder*="邮箱"], input[placeholder*="会员"], '
    'input[placeholder*="请输入"], input[placeholder*="Account"], input[placeholder*="Username"], '
    'input[type="text"]:not([placeholder*="验证"]):not([placeholder*="验证码"]):not([placeholder*="搜"]), '
    'input[type="tel"], input[type="email"], input[autocomplete="username"]'
)


def _is_nav_error(exc: BaseException) -> bool:
    msg = str(exc or "").lower()
    return any(m in msg for m in _NAV_ERR_MARKERS) or "target closed" in msg or "has been closed" in msg


async def _wait_nav_settle(page, timeout_ms: int = 8000) -> None:
    """页面跳转后等 DOM 就绪，吞掉导航竞争错误。"""
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    except Exception:
        pass
    try:
        await page.wait_for_timeout(200)
    except Exception:
        pass


async def _safe_query(page, selector: str):
    """query_selector，导航中返回 None 而不是抛错。"""
    try:
        if page.is_closed():
            return None
    except Exception:
        return None
    try:
        return await page.query_selector(selector)
    except Exception as e:
        if _is_nav_error(e):
            await _wait_nav_settle(page)
            try:
                if page.is_closed():
                    return None
                return await page.query_selector(selector)
            except Exception:
                return None
        raise


async def _safe_query_all(page, selector: str) -> list:
    try:
        if page.is_closed():
            return []
    except Exception:
        return []
    try:
        return await page.query_selector_all(selector)
    except Exception as e:
        if _is_nav_error(e):
            await _wait_nav_settle(page)
            try:
                return await page.query_selector_all(selector)
            except Exception:
                return []
        raise


async def _find_password_in_frames(page, timeout_ms: int = 12000):
    """主文档 + iframe 里找密码框（平博/综合站常见）。"""
    deadline = time.time() + max(1.0, timeout_ms / 1000.0)
    while time.time() < deadline:
        try:
            if page.is_closed():
                return None, None
        except Exception:
            return None, None
        try:
            for frame in page.frames:
                try:
                    el = await frame.query_selector(PWD_SELECTOR)
                    if el:
                        return frame, el
                except Exception as e:
                    if _is_nav_error(e):
                        continue
                    continue
        except Exception:
            pass
        await page.wait_for_timeout(350)
    return None, None


async def _page_looks_logged_in(page, *, auth_mode: str = "session", captured: Optional[dict] = None) -> bool:
    """严格判断是否已登录（禁止仅因「首页无密码框」就跳过填表）。"""
    try:
        if page.is_closed():
            return False
    except Exception:
        return False
    if captured and captured.get("token"):
        return True
    if auth_mode == "kaiyun":
        try:
            tok = await page.evaluate("() => localStorage.getItem('X-API-TOKEN') || ''")
            if tok:
                return True
        except Exception:
            pass
    try:
        body = await page.inner_text("body", timeout=2500)
    except Exception:
        return False
    # 仍有明显登录表单 → 未登录
    if any(x in body for x in ("请输入密码", "请输入账号", "忘记密码", "立即注册")):
        # 同时有退出才算已登录
        if not any(x in body for x in ("退出", "登出", "Logout", "Sign out")):
            return False
    if any(x in body for x in ("退出", "登出", "Logout", "Sign out", "安全退出")):
        return True
    if "余额" in body and ("¥" in body or "￥" in body or "元" in body):
        return True
    return False


async def _click_login_entry(page) -> bool:
    """首页点「登录」入口展开表单。"""
    texts = ("登录", "登 录", "登入", "Sign in", "Log in", "LOGIN", "会员登录")
    for t in texts:
        for sel in (
            f'a:has-text("{t}")',
            f'button:has-text("{t}")',
            f'div[role="button"]:has-text("{t}")',
            f'span:has-text("{t}")',
        ):
            try:
                loc = page.locator(sel).first
                if await loc.count() == 0:
                    continue
                await loc.click(timeout=2500, force=True)
                await _wait_nav_settle(page, 5000)
                return True
            except Exception:
                continue
    return False


async def _open_login_form(page, base_url: str, login_paths: list[str], nav_timeout_ms: int) -> tuple[bool, str]:
    """多路径打开登录页；遇到 404 跳过。返回 (ok, last_err)。"""
    last_err = ""
    origin = site_origin(base_url) or (base_url or "").rstrip("/")
    paths = list(login_paths or ["/"])
    if "/" not in paths:
        paths.append("/")
    # 首页优先，减少乱跳
    paths = sorted(paths, key=lambda p: 0 if p in ("/", "") else 1)

    for path in paths:
        try:
            url = path if str(path).startswith("http") else f"{origin}{path}"
            await page.goto(url, wait_until="domcontentloaded", timeout=nav_timeout_ms)
            await _wait_nav_settle(page, 4000)
            # 404 不继续在此页点登录
            try:
                from app.services.bookmakers.venue_entry import page_looks_like_404

                if await page_looks_like_404(page):
                    last_err = f"404 on {url}"
                    continue
            except Exception:
                pass
            frame, el = await _find_password_in_frames(page, timeout_ms=10000)
            if el:
                return True, ""
            await page.wait_for_timeout(800)
            frame, el = await _find_password_in_frames(page, timeout_ms=4000)
            if el:
                return True, ""
            if await _click_login_entry(page):
                frame, el = await _find_password_in_frames(page, timeout_ms=10000)
                if el:
                    return True, ""
            last_err = f"no password on {url}"
        except Exception as e:
            last_err = str(e)
            if _is_nav_error(e):
                await _wait_nav_settle(page)
                try:
                    _, el = await _find_password_in_frames(page, timeout_ms=6000)
                    if el:
                        return True, ""
                except Exception:
                    pass
            continue

    try:
        await page.goto(origin + "/", wait_until="domcontentloaded", timeout=nav_timeout_ms)
        await _wait_nav_settle(page)
        await _click_login_entry(page)
        _, el = await _find_password_in_frames(page, timeout_ms=12000)
        if el:
            return True, ""
    except Exception as e:
        last_err = str(e)

    return False, last_err


async def interactive_site_login(
    *,
    base_url: str,
    username: str,
    password: str,
    session_token: str = "",
    wait_seconds: int = 60,
    nav_timeout_ms: int = 20000,
    keep_alive: bool = False,
    force_new: bool = False,
    site_code: str = "ob",
    manual_venue: bool = False,
) -> dict:
    """
    弹出 Chromium，打开站点登录页，预填账号密码。
    - OB(kaiyun)：检测 X-API-TOKEN
    - 平博：登录成功后保存 cookies+localStorage 会话快照

    keep_alive=True：验证成功后保持窗口可见并长连接（供实时刷新）。
    keep_alive=False：验证成功后关闭浏览器。
    force_new=False：若已有同站长连接则直接复用，不再弹窗。

    返回: {ok, message, token, profile, balance, kept_alive?}
    """
    from playwright.async_api import async_playwright

    from app.services.bookmakers.session_blob import (
        apply_session_blob,
        capture_session_token,
    )
    from app.services.bookmakers.site_profiles import get_site_profile
    from app.services.bookmakers.site_session import site_sessions

    site_code = (site_code or "ob").lower()
    profile_cfg = get_site_profile(site_code)
    auth_mode = str(profile_cfg.get("auth_mode") or "session")
    token_keys = list(profile_cfg.get("token_storage_keys") or ["X-API-TOKEN"])

    base_url = (base_url or "").rstrip("/")
    home_url = site_origin(base_url) or base_url
    login_paths = list(profile_cfg.get("login_paths") or ["/user/login"])
    headed = True
    from app.services.bookmakers.site_profiles import needs_manual_venue as _needs_manual

    # 手动场馆：登录后需在可见窗口中进入盘口
    if _needs_manual(site_code) or manual_venue or (os.getenv("BOOKMAKER_MANUAL_VENUE") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        manual_venue = True
    else:
        manual_venue = False
    captured: dict[str, Any] = {"token": None, "profiles": []}
    nav_timeout_ms = max(8000, min(int(nav_timeout_ms or 20000), 30000))
    wait_seconds = max(20, min(int(wait_seconds or 90), 120))

    # 已有长连接：优先复用同一 Chromium（禁止一站多窗）
    if keep_alive and not force_new:
        existing = site_sessions.find(base_url=base_url, site_code=site_code)
        if existing and existing.page and not existing.page.is_closed():
            reuse_ok = True
            if site_code == "pinnacle":
                try:
                    from app.services.bookmakers.plugins.pinnacle.venue import pinnacle_session_expired

                    reuse_ok = not await pinnacle_session_expired(existing.page)
                except Exception:
                    reuse_ok = False
            if _needs_manual(site_code):
                try:
                    from app.services.bookmakers.venue_entry import is_in_sportsbook

                    reuse_ok = reuse_ok and await is_in_sportsbook(existing.page)
                except Exception:
                    reuse_ok = False
            if reuse_ok:
                probe = await site_sessions.probe_alive(
                    existing.base_url or base_url,
                    session_token=session_token or existing.token,
                    touch_sport=False,
                    site_code=site_code,
                    fast=True,
                )
                if probe.get("ok"):
                    try:
                        vu = existing.venue_url or (existing.page.url or "")
                    except Exception:
                        vu = existing.venue_url or ""
                    profile = {"name": username} if username else {}
                    if vu:
                        profile["venue_url"] = vu
                    return {
                        "ok": True,
                        "message": "已复用长连接（已在体育场馆），未再次弹窗",
                        "token": probe.get("token") or existing.token or session_token,
                        "profile": profile,
                        "balance": 0,
                        "kept_alive": True,
                        "reused": True,
                    }
            # 未在盘口：复用同窗继续登录流程，绝不 invalidate 再 launch
            logger.info(
                "interactive_site_login reuse window site=%s key=%s",
                site_code,
                getattr(existing, "key", ""),
            )
            pw = existing.playwright
            browser = existing.browser
            context = existing.context
            page = existing.page
            # 跳过下面的 launch，直接走填表/进馆
            reused_window = True
        else:
            reused_window = False
    else:
        reused_window = False

    if not reused_window:
        # 同站若仍有旧会话，先关掉再开唯一新窗；launch 必须在锁内完成
        from app.services.bookmakers.site_session import _session_key as _sk

        launch_key = _sk(base_url)
        async with site_sessions._launch_lock(launch_key):
            again = site_sessions.find(base_url=base_url, site_code=site_code)
            if again and again.page and not again.page.is_closed():
                reused_window = True
                own_browser = False
                pw = again.playwright
                browser = again.browser
                context = again.context
                page = again.page
            else:
                await site_sessions._evict_duplicates(
                    key=launch_key,
                    site_code=site_code,
                    keep=None,
                )
                from app.services.bookmakers.browser_runtime import launch_headed_chromium

                pw = await async_playwright().start()
                # macOS Crashpad/xattr 会导致默认 launch SIGABRT；加固启动，不改后续进馆/下单逻辑
                result = await launch_headed_chromium(pw, maximized=True)
                if hasattr(result, 'new_context'):
                    # Browser: 需要创建 context
                    browser = result
                    context = await browser.new_context(
                        ignore_https_errors=True,
                        locale="zh-CN",
                        viewport={"width": 1280, "height": 860},
                        user_agent=DESKTOP_UA,
                    )
                else:
                    # BrowserContext (persistent): 直接使用
                    browser = None
                    context = result
                page = await context.new_page()
                own_browser = True
                # 占位登记，防止锁外并发再 launch（token 稍后 adopt 覆盖）
                await site_sessions.adopt_login_browser(
                    base_url=base_url,
                    token=session_token or "",
                    playwright=pw,
                    browser=browser,
                    context=context,
                    page=page,
                    venue_url="",
                    site_code=site_code,
                )
    else:
        own_browser = False

    # NOTE: helpers below close browser only when we own the launch

    async def _fail(msg: str) -> dict:
        # 进馆失败也尽量保留窗口，方便用户手动进馆后再点验证；
        # 仅在明确未 keep_alive 且我们拥有浏览器时才关闭。
        if own_browser and not keep_alive:
            try:
                await site_sessions.invalidate(base_url)
            except Exception:
                pass
            try:
                if browser:
                    await browser.close()
            except Exception:
                pass
            try:
                await pw.stop()
            except Exception:
                pass
        elif own_browser and keep_alive:
            # 已弹窗：登记占位，勿关掉；用户可继续进馆
            try:
                await site_sessions.adopt_login_browser(
                    base_url=base_url,
                    token=session_token or "",
                    playwright=pw,
                    browser=browser,
                    context=context,
                    page=page,
                    venue_url="",
                    site_code=site_code,
                )
            except Exception:
                pass
            logger.warning("login/venue fail but keep browser open: %s", msg)
        return {
            "ok": False,
            "message": msg,
            "token": "",
            "profile": {},
            "balance": 0,
            "kept_alive": bool(own_browser and keep_alive),
        }

    async def _success(tok: str, profile: dict, balance, message: str) -> dict:
        nonlocal page, own_browser, context
        # 登录后进场馆：默认自动点击；手动模式则干等
        try:
            from app.services.bookmakers.site_profiles import needs_manual_venue
            from app.services.bookmakers.venue_entry import enter_portal_venue, is_in_sportsbook

            if page is not None:
                use_manual = needs_manual_venue(site_code) or manual_venue
                page, venue_url = await enter_portal_venue(
                    page,
                    site_code=site_code,
                    base_url=base_url,
                    context=context,
                    timeout_ms=max(60000, int(wait_seconds * 1000)),
                    force=True,
                    manual_venue=use_manual,
                    wait_manual=use_manual,
                )
                # 手动进馆：信任用户已进入场馆，跳过 is_in_sportsbook 检查
                # 自动进馆：检查是否成功进入盘口
                if not use_manual:
                    if not await is_in_sportsbook(page):
                        return await _fail(
                            "自动进入场馆失败。请检查站点账号/URL，或设置 BOOKMAKER_MANUAL_VENUE=1 手动进入"
                        )
                try:
                    vu = venue_url or (page.url or "")
                except Exception:
                    vu = venue_url or ""
                if vu:
                    profile = dict(profile or {})
                    profile["venue_url"] = vu
                    profile.setdefault("name", username)
                tag = "已手动进入场馆" if use_manual else "已自动进入场馆"
                if "场馆" not in (message or "") and "盘口" not in (message or ""):
                    message = (message or "登录成功") + f"；{tag}"
        except Exception as e:
            logger.warning("enter venue after login (%s): %s", site_code, e)
            from app.services.bookmakers.site_profiles import needs_manual_venue

            if not (needs_manual_venue(site_code) or manual_venue):
                return await _fail(f"自动进入场馆失败: {e}")
            # 手动模式：进馆异常不关闭浏览器，继续尝试 keep_alive

        if keep_alive:
            try:
                # 始终保持窗口可见（禁止最小化/隐藏，否则滚球停推）
                vu = ""
                if isinstance(profile, dict):
                    vu = str(profile.get("venue_url") or "")
                if not vu and page is not None:
                    try:
                        vu = page.url or ""
                    except Exception:
                        vu = ""
                # 进馆后直接接管；场馆 URL 动态捕获（不写死路径）
                try:
                    from app.services.bookmakers.venue_entry import capture_live_venue_url

                    live = await capture_live_venue_url(page)
                    if live:
                        vu = live
                        # 切到包含盘口的标签
                        ctx = context or getattr(page, "context", None)
                        if ctx is not None:
                            for p in reversed(list(ctx.pages)):
                                try:
                                    if not p.is_closed() and (p.url or "") == live:
                                        page = p
                                        break
                                except Exception:
                                    continue
                except Exception:
                    pass
                await site_sessions.adopt_login_browser(
                    base_url=base_url,
                    token=tok,
                    playwright=pw,
                    browser=browser,
                    context=context or getattr(page, "context", None),
                    page=page,
                    venue_url=vu,
                    site_code=site_code,
                )
                # 禁止在 keep_alive 成功后关闭浏览器
                own_browser = False
                return {
                    "ok": True,
                    "message": message or "登录成功，浏览器保持可见长连接（便于拉取真实盘口）",
                    "token": tok,
                    "profile": profile or {"name": username},
                    "balance": float(balance),
                    "kept_alive": True,
                }
            except Exception as e:
                logger.exception("adopt keep-alive failed")
                # 不关闭浏览器：用户已登录进馆，保留窗口供手动操作
                return {
                    "ok": True,
                    "message": f"登录成功，但长连接建立失败: {e}",
                    "token": tok,
                    "profile": profile or {"name": username},
                    "balance": float(balance),
                    "kept_alive": False,
                }
        if own_browser:
            try:
                await browser.close()
            except Exception:
                pass
            try:
                await pw.stop()
            except Exception:
                pass
        return {
            "ok": True,
            "message": message or "登录成功，浏览器已关闭",
            "token": tok,
            "profile": profile or {"name": username},
            "balance": float(balance),
            "kept_alive": False,
        }

    try:
        if reused_window:
            # 复用已有窗口：仅在尚未进场馆时回首页；已在 OB 盘口则禁止 goto /（否则会被弹回首页）
            # 平博：禁止 bring_to_front（同步/保活时会把 Chromium 抢到系统前台）
            assert page is not None and context is not None
            stay_put = False
            try:
                from app.services.bookmakers.venue_entry import _is_venue_url, is_in_sportsbook

                cur_u = page.url or ""
                stay_put = await is_in_sportsbook(page) or _is_venue_url(cur_u)
            except Exception:
                stay_put = False
            code_l = (site_code or "").lower()
            if not stay_put and code_l != "pinnacle":
                try:
                    await page.bring_to_front()
                except Exception:
                    pass
            if not stay_put:
                try:
                    await page.goto(home_url + "/", wait_until="domcontentloaded", timeout=nav_timeout_ms)
                except Exception:
                    pass
            else:
                logger.info(
                    "interactive_site_login reuse stay in venue site=%s url=%s",
                    site_code,
                    (getattr(page, "url", "") or "")[:120],
                )

        async def on_response(response):
            try:
                url = response.url
                if any(
                    x in url
                    for x in (
                        "/user/member/info",
                        "/user/getUserInfo",
                        "/user/login",
                        "getUserInfo",
                        "wallet",
                        "balance",
                        "member",
                        "login",
                        "auth",
                    )
                ):
                    try:
                        body = await response.json()
                    except Exception:
                        return
                    if isinstance(body, dict):
                        data = _unwrap_payload(body)
                        tok = _pick(data, "token", "X-API-TOKEN", "accessToken", "access_token") or body.get(
                            "token"
                        )
                        if tok:
                            captured["token"] = tok
                        captured["profiles"].append(body)
            except Exception:
                return

        page.on("response", on_response)

        async def _finalize_success(message: str) -> dict:
            tok = await capture_session_token(context, page, token_keys)
            if auth_mode == "kaiyun":
                try:
                    pure = await page.evaluate("() => localStorage.getItem('X-API-TOKEN') || ''")
                except Exception as e:
                    if _is_nav_error(e):
                        await _wait_nav_settle(page)
                        pure = await page.evaluate("() => localStorage.getItem('X-API-TOKEN') || ''")
                    else:
                        pure = ""
                if pure:
                    tok = pure
                elif captured.get("token"):
                    tok = str(captured["token"])
            profile, balance = await _read_profile(page, captured, base_url, username)
            result = await _success(tok, profile, balance, message)
            # 进馆后重新抓取余额（从体育场馆页面读取）
            try:
                from app.services.bookmakers.venue_entry import is_in_sportsbook
                if result.get("ok") and page and not page.is_closed() and await is_in_sportsbook(page):
                    _, bal_venue = await _read_profile(page, captured, base_url, username)
                    if bal_venue and bal_venue > 0:
                        result["balance"] = float(bal_venue)
            except Exception:
                pass
            return result

        # 已有会话：仅在确认已登录时复用；否则继续打开登录框并填写账号密码
        if session_token:
            try:
                await page.goto(home_url + "/", wait_until="domcontentloaded", timeout=nav_timeout_ms)
                await apply_session_blob(context, page, session_token)
                await page.reload(wait_until="domcontentloaded", timeout=nav_timeout_ms)
                await _wait_nav_settle(page)
            except Exception as e:
                logger.warning("session restore navigate: %s", e)
            if await _page_looks_logged_in(page, auth_mode=auth_mode, captured=captured):
                if auth_mode == "kaiyun":
                    try:
                        tok = await page.evaluate("() => localStorage.getItem('X-API-TOKEN') || ''")
                    except Exception:
                        tok = str(captured.get("token") or session_token or "")
                    if tok:
                        captured["token"] = tok
                        profile, balance = await _read_profile(page, captured, base_url, username)
                        result = await _success(
                            tok,
                            profile,
                            balance,
                            "会话有效，浏览器保持可见长连接"
                            if keep_alive
                            else "会话有效，浏览器已关闭",
                        )
                        # 进馆后重新抓取余额（从体育场馆页面读取）
                        try:
                            from app.services.bookmakers.venue_entry import is_in_sportsbook
                            if result.get("ok") and page and not page.is_closed() and await is_in_sportsbook(page):
                                _, bal_venue = await _read_profile(page, captured, base_url, username)
                                if bal_venue and bal_venue > 0:
                                    result["balance"] = float(bal_venue)
                        except Exception:
                            pass
                        return result
                else:
                    return await _finalize_success(
                        "会话有效，浏览器保持可见长连接" if keep_alive else "会话有效，浏览器已关闭"
                    )
            logger.info("session present but not logged in — will fill username/password")

        if not (username and password):
            return await _fail("缺少账号或密码，无法自动填写登录表单")

        opened, last_err = await _open_login_form(page, home_url, login_paths, nav_timeout_ms)
        if not opened:
            # 再试一次：回首页点登录
            try:
                await page.goto(home_url + "/", wait_until="domcontentloaded", timeout=nav_timeout_ms)
                await _click_login_entry(page)
                frame, el = await _find_password_in_frames(page, timeout_ms=8000)
                opened = el is not None
            except Exception as e:
                last_err = str(e)
        if not opened:
            return await _fail(f"无法打开登录页/登录框（{last_err[:120]}）")

        filled = await _fill_credentials(page, username, password)
        if not filled:
            # 可能登录框被关掉：再点一次登录入口后重填
            await _click_login_entry(page)
            await page.wait_for_timeout(500)
            filled = await _fill_credentials(page, username, password)
        if not filled:
            return await _fail("未能自动填写账号密码。请确认页面已弹出登录框后重试")

        await page.wait_for_timeout(300)
        await _click_login_submit(page)
        await _wait_nav_settle(page, 6000)

        # 250ms 轮询；导航中吞掉 context destroyed，继续等
        loops = max(1, int(wait_seconds * 4))
        logged_in = False
        for i in range(loops):
            await page.wait_for_timeout(250)
            try:
                if auth_mode == "kaiyun":
                    tok = await page.evaluate("() => localStorage.getItem('X-API-TOKEN') || ''")
                    if tok:
                        captured["token"] = tok
                        logged_in = True
                        break
                else:
                    if captured.get("token"):
                        logged_in = True
                        break
                    pwd = await _safe_query(page, PWD_SELECTOR)
                    try:
                        url_l = (page.url or "").lower()
                    except Exception:
                        url_l = ""
                    if pwd is None and ("login" not in url_l and "signin" not in url_l):
                        logged_in = True
                        break
                    if i % 4 == 3:
                        try:
                            body = await page.inner_text("body")
                            if any(
                                x in body
                                for x in ("退出", "登出", "余额", "钱包", "Logout", "Sign out")
                            ):
                                logged_in = True
                                break
                        except Exception as e:
                            if _is_nav_error(e):
                                await _wait_nav_settle(page)
                            continue
            except Exception as e:
                if _is_nav_error(e):
                    await _wait_nav_settle(page)
                    continue
                raise

        if not logged_in and not captured.get("token"):
            return await _fail("等待登录超时。请在弹出的浏览器中完成登录/人机验证后重试")

        try:
            await page.wait_for_timeout(300)
        except Exception:
            pass
        return await _finalize_success(
            "登录成功，浏览器保持可见长连接" if keep_alive else "登录成功，浏览器已关闭"
        )
    except Exception as e:
        logger.exception("interactive_site_login failed")
        return await _fail(" ".join(str(f"浏览器登录失败: {e}").split()))


async def _fill_input_value(locator, value: str) -> bool:
    """稳健填写：click → fill → 校验；失败再逐字输入。"""
    try:
        if await locator.count() == 0:
            return False
        await locator.first.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    try:
        await locator.first.click(timeout=4000)
    except Exception:
        pass
    try:
        await locator.first.fill("", timeout=3000)
    except Exception:
        pass
    try:
        await locator.first.fill(value, timeout=5000)
        try:
            got = await locator.first.input_value(timeout=2000)
            if got == value:
                return True
        except Exception:
            return True
    except Exception:
        pass
    try:
        await locator.first.click(timeout=3000)
        await locator.first.press("Control+A")
        await locator.first.press_sequentially(value, delay=25)
        try:
            got = await locator.first.input_value(timeout=2000)
            return got == value or len(got or "") > 0
        except Exception:
            return True
    except Exception as e:
        logger.debug("fill input failed: %s", e)
        return False


async def _fill_credentials(page, username: str, password: str) -> bool:
    """
    自动填写账号密码。优先用 locator（避免 DOM 节点 detached）。
    返回是否填写成功。
    """
    username = (username or "").strip()
    password = password or ""
    if not username or not password:
        logger.warning("fill credentials skipped: empty username/password")
        return False

    await _wait_nav_settle(page, 2000)
    # 确保登录框可见
    frame, pwd_el = await _find_password_in_frames(page, timeout_ms=4000)
    if not pwd_el:
        await _click_login_entry(page)
        await page.wait_for_timeout(600)
        frame, pwd_el = await _find_password_in_frames(page, timeout_ms=8000)
    if not pwd_el:
        logger.warning("fill credentials: password input not found")
        return False

    scopes = []
    if frame is not None:
        scopes.append(frame)
    scopes.append(page)

    user_ok = False
    pwd_ok = False

    for scope in scopes:
        try:
            user_loc = scope.locator(USER_SELECTOR)
            pwd_loc = scope.locator(PWD_SELECTOR)
            if await pwd_loc.count() == 0:
                continue
            # 账号：取第一个可见文本框
            n = await user_loc.count()
            for i in range(min(n, 6)):
                loc = user_loc.nth(i)
                try:
                    if not await loc.is_visible(timeout=800):
                        continue
                except Exception:
                    continue
                # 跳过验证码框
                try:
                    ph = (await loc.get_attribute("placeholder") or "") + (await loc.get_attribute("name") or "")
                    if any(x in ph for x in ("验证", "验证码", "captcha", "code", "搜")):
                        continue
                except Exception:
                    pass
                if await _fill_input_value(loc, username):
                    user_ok = True
                    break
            if await _fill_input_value(pwd_loc.first, password):
                pwd_ok = True
            if user_ok and pwd_ok:
                logger.info("credentials filled ok (scope=%s)", getattr(scope, "name", "page"))
                return True
        except Exception as e:
            if _is_nav_error(e):
                await _wait_nav_settle(page)
                continue
            logger.debug("fill in scope failed: %s", e)
            continue

    # 最后兜底：JS 写入可见 input
    if not (user_ok and pwd_ok):
        try:
            ok = await page.evaluate(
                """({ user, pass }) => {
                  const isVisible = (el) => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
                  const inputs = Array.from(document.querySelectorAll('input')).filter(isVisible);
                  let u = inputs.find(i => {
                    const t = (i.type || 'text').toLowerCase();
                    const p = (i.placeholder || '') + (i.name || '') + (i.id || '');
                    return (t === 'text' || t === 'tel' || t === 'email' || t === '')
                      && !/验证|验证码|captcha|搜/.test(p);
                  });
                  let p = inputs.find(i => (i.type || '').toLowerCase() === 'password');
                  if (!u || !p) return false;
                  const setVal = (el, v) => {
                    el.focus();
                    el.value = v;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                  };
                  setVal(u, user);
                  setVal(p, pass);
                  return u.value === user && p.value === pass;
                }""",
                {"user": username, "pass": password},
            )
            if ok:
                logger.info("credentials filled via JS fallback")
                return True
        except Exception as e:
            logger.debug("js fill fallback failed: %s", e)

    logger.warning("credentials fill incomplete user_ok=%s pwd_ok=%s", user_ok, pwd_ok)
    return bool(user_ok and pwd_ok)


async def _click_login_submit(page) -> None:
    """点击登录提交按钮（避免点到顶栏「登录」入口）。"""
    submit_sels = (
        'form button:has-text("登录")',
        'form button:has-text("登 录")',
        'form button:has-text("登入")',
        'form button[type="submit"]',
        'button[type="submit"]:has-text("登录")',
        'button.login-btn',
        'button:has-text("立即登录")',
        'button:has-text("登录")',
        'button:has-text("登 录")',
        'button:has-text("登入")',
        'button:has-text("Sign in")',
        'button:has-text("Log in")',
        'div[role="button"]:has-text("登录")',
    )
    # 优先在含密码框的 frame 内点
    frames = []
    try:
        frames = list(page.frames)
    except Exception:
        frames = []
    for frame in frames:
        try:
            if await frame.locator(PWD_SELECTOR).count() == 0:
                continue
        except Exception:
            continue
        for sel in submit_sels:
            try:
                btn = frame.locator(sel).first
                if await btn.count() == 0:
                    continue
                await btn.click(timeout=3500)
                return
            except Exception:
                continue
    for sel in submit_sels:
        try:
            btn = page.locator(sel).first
            if await btn.count() == 0:
                continue
            # 跳过顶栏导航里单纯打开登录框的链接（无附近密码框时仍可点，作为兜底）
            await btn.click(timeout=3500)
            return
        except Exception as e:
            if _is_nav_error(e):
                await _wait_nav_settle(page)
            continue
    try:
        await page.keyboard.press("Enter")
    except Exception:
        pass


async def _click_login(page) -> None:
    """兼容旧名。"""
    await _click_login_submit(page)


async def _read_profile(page, captured: dict, base_url: str, username: str) -> tuple[dict, Decimal]:
    profile: dict = {}
    balance = Decimal("0")
    for body in reversed(captured.get("profiles") or []):
        if not isinstance(body, dict):
            continue
        p = extract_profile(body, username)
        if p.get("name") or p.get("member_id") or "balance" in p:
            profile = p
            if "balance" in p:
                balance = _to_decimal(p["balance"])
            break

    token = captured.get("token") or ""
    if token:
        try:
            result = await page.evaluate(
                """async (args) => {
                  const { token, base } = args;
                  try {
                    const resp = await fetch(base + '/site/api/v1/user/member/info', {
                      method: 'POST',
                      headers: {
                        'Content-Type': 'application/json',
                        'Accept': 'application/json',
                        'X-API-CLIENT': 'web',
                        'X-API-VERSION': '2.0.0',
                        'X-API-SITE': '4002',
                        'X-API-TOKEN': token,
                        'X-API-UUID': (localStorage.getItem('_uuid') || ''),
                        'X-API-XXX': '',
                      },
                      body: JSON.stringify({}),
                      credentials: 'include',
                    });
                    return await resp.json();
                  } catch (e) { return null; }
                }""",
                {"token": token, "base": base_url},
            )
            if isinstance(result, dict):
                p = extract_profile(result, username)
                if p:
                    profile = {**profile, **p}
                    if "balance" in p:
                        balance = _to_decimal(p["balance"])
        except Exception as e:
            if not _is_nav_error(e):
                logger.warning("read profile fetch failed: %s", e)

    if balance <= 0:
        try:
            text = await page.inner_text("body")
            text = (text or "").replace(",", "").replace("\u00a0", " ")
            m = (
                re.search(r"([0-9]+(?:\.[0-9]+)?)\s*CNY\b", text, re.I)
                or re.search(r"\bCNY\s*([0-9]+(?:\.[0-9]+)?)", text, re.I)
                or re.search(r"[¥￥]\s*([0-9]+(?:\.[0-9]+)?)", text)
                or re.search(r"余\s*额\s*[:：]?\s*([0-9]+(?:\.[0-9]+)?)", text)
                or re.search(r"Balance\s*[:：]?\s*([0-9]+(?:\.[0-9]+)?)", text, re.I)
            )
            if m:
                balance = _to_decimal(m.group(1))
                profile.setdefault("balance", float(balance))
        except Exception:
            pass
    return profile, balance
