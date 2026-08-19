"""
本机 Browser Gate 站点长连接：

验证成功后保留 Playwright 会话，窗口始终可见。
规则（强制）：
- 每个站点（site_code / 规范化 host）最多 1 个 Chromium
- 已在盘口：禁止 reload/goto 闪烁
- 掉线：最多 120s 软恢复一次 venue
- ensure_for_odds / balance：禁止再 launch 新浏览器（只复用登录会话）
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


def _norm_host(host: str) -> str:
    h = (host or "").lower().strip()
    if h.startswith("www."):
        h = h[4:]
    return h


def _session_key(base_url: str) -> str:
    """同一站点统一 key：忽略 www / 路径 / 默认端口差异。"""
    raw = (base_url or "").strip().rstrip("/")
    try:
        u = urlparse(raw if "://" in raw else f"https://{raw}")
        host = _norm_host(u.hostname or raw)
        scheme = (u.scheme or "https").lower()
        port = u.port or (443 if scheme == "https" else 80)
        return f"{scheme}://{host}:{port}"
    except Exception:
        return _norm_host(raw)


@dataclass
class KeptSiteSession:
    key: str
    base_url: str
    token: str
    playwright: Any
    browser: Any
    context: Any
    page: Any
    created_at: float = field(default_factory=time.time)
    last_refresh_at: float = 0.0
    hidden: bool = False
    venue_url: str = ""
    site_code: str = ""
    last_balance: float = 0.0
    balance_recognized: bool = False
    last_restore_at: float = 0.0
    # 重新验证会主动关闭旧 Chromium。该关闭不是用户退出，不能通知后端
    # 把正在建立的新连接标记成 DISCONNECTED。
    suppress_disconnect_notify: bool = False

    @property
    def age_sec(self) -> float:
        return time.time() - self.created_at


class SiteSessionManager:
    def __init__(self) -> None:
        self._sessions: dict[str, KeptSiteSession] = {}
        self._lock = asyncio.Lock()
        # 防止登录与盘口同步并发各 launch 一个浏览器
        self._launch_locks: dict[str, asyncio.Lock] = {}

    def _launch_lock(self, key: str) -> asyncio.Lock:
        lock = self._launch_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._launch_locks[key] = lock
        return lock

    def _page_alive(self, sess: Optional[KeptSiteSession]) -> bool:
        if not sess or not sess.page:
            return False
        try:
            return not sess.page.is_closed()
        except Exception:
            return False

    def get(self, base_url: str) -> Optional[KeptSiteSession]:
        return self._sessions.get(_session_key(base_url))

    def get_by_site_code(self, site_code: str) -> Optional[KeptSiteSession]:
        code = (site_code or "").lower().strip()
        if not code:
            return None
        for sess in self._sessions.values():
            if (sess.site_code or "").lower() == code and self._page_alive(sess):
                return sess
        return None

    def find(self, *, base_url: str = "", site_code: str = "") -> Optional[KeptSiteSession]:
        """按 base_url 或 site_code 查找存活会话（优先 base_url）。"""
        if base_url:
            sess = self.get(base_url)
            if self._page_alive(sess):
                return sess
        if site_code:
            return self.get_by_site_code(site_code)
        return None

    def browser_count(self) -> int:
        n = 0
        for sess in self._sessions.values():
            try:
                if sess.browser and getattr(sess.browser, "is_connected", lambda: True)():
                    n += 1
                elif self._page_alive(sess):
                    # launch_persistent_context 没有单独的 Browser 句柄，但仍是
                    # 一个真实可见的 Chromium 会话。
                    n += 1
            except Exception:
                if sess.page and not sess.page.is_closed():
                    n += 1
        return n

    async def close(self, base_url: str) -> None:
        key = _session_key(base_url)
        async with self._lock:
            sess = self._sessions.pop(key, None)
        if sess:
            await self._dispose(sess)

    async def close_all(self) -> None:
        async with self._lock:
            items = list(self._sessions.values())
            self._sessions.clear()
        for sess in items:
            await self._dispose(sess)

    async def _dispose(self, sess: KeptSiteSession) -> None:
        # 先关 context/page，再 browser，再 playwright，尽量杀干净残留窗口
        for closer in (getattr(sess, "page", None), getattr(sess, "context", None), sess.browser, sess.playwright):
            try:
                if closer is None:
                    continue
                if hasattr(closer, "close"):
                    await closer.close()
                elif hasattr(closer, "stop"):
                    await closer.stop()
            except Exception:
                pass

    async def _evict_duplicates(self, *, key: str, site_code: str = "", keep: Optional[KeptSiteSession] = None) -> None:
        """同 key / 同 site_code 只留一个。"""
        code = (site_code or "").lower()
        doomed: list[KeptSiteSession] = []
        async with self._lock:
            for k, sess in list(self._sessions.items()):
                if keep is not None and sess is keep:
                    continue
                same_key = k == key
                same_code = code and (sess.site_code or "").lower() == code
                if same_key or same_code:
                    doomed.append(self._sessions.pop(k))
        for sess in doomed:
            sess.suppress_disconnect_notify = True
            logger.warning(
                "evict duplicate browser key=%s site=%s",
                sess.key,
                sess.site_code or "?",
            )
            await self._dispose(sess)

    async def adopt_login_browser(
        self,
        *,
        base_url: str,
        token: str,
        playwright,
        browser,
        context,
        page,
        venue_url: str = "",
        site_code: str = "",
    ) -> KeptSiteSession:
        """登录成功后接管浏览器：登记长连接；同站其它窗口关闭，当前窗不销毁。"""
        key = _session_key(base_url)
        code = (site_code or "").lower()
        doomed: list[KeptSiteSession] = []
        async with self._lock:
            for k, old in list(self._sessions.items()):
                same_browser = old.browser is browser or old.page is page
                same_key = k == key
                same_code = code and (old.site_code or "").lower() == code
                if same_browser:
                    # 同一 Chromium 从“登录占位”升级为“已登录会话”时会创建
                    # 新会话对象。旧对象上的监听器仍会随浏览器关闭触发，必须
                    # 标记为陈旧，否则下次重新验证会误发 browser-closed。
                    old.suppress_disconnect_notify = True
                    self._sessions.pop(k, None)
                    continue
                if same_key or same_code:
                    doomed.append(self._sessions.pop(k))

        for old in doomed:
            old.suppress_disconnect_notify = True
            logger.warning(
                "adopt close other browser key=%s site=%s",
                old.key,
                old.site_code or "?",
            )
            await self._dispose(old)

        cur = ""
        try:
            cur = page.url or ""
        except Exception:
            cur = ""
        sess = KeptSiteSession(
            key=key,
            base_url=base_url.rstrip("/"),
            token=token,
            playwright=playwright,
            browser=browser,
            context=context,
            page=page,
            last_refresh_at=time.time(),
            hidden=False,
            venue_url=(venue_url or cur or "").strip(),
            site_code=code,
        )
        async with self._lock:
            self._sessions[key] = sess

        # 监听浏览器/页面关闭事件：用户手动关闭浏览器时自动清理会话
        self._setup_browser_listeners(sess)

        logger.info(
            "site session kept visible: %s site=%s venue=%s browsers=%s",
            key,
            code or "?",
            (sess.venue_url or "")[:120],
            self.browser_count(),
        )
        return sess

    def _setup_browser_listeners(self, sess: KeptSiteSession) -> None:
        """设置浏览器/页面关闭事件监听（带代数，避免换页后重复回调误伤）。"""
        gen = int(getattr(sess, "_listener_gen", 0) or 0) + 1
        sess._listener_gen = gen
        page = sess.page
        browser = sess.browser
        context = sess.context
        if page is not None:
            try:
                page.on(
                    "close",
                    lambda *_args, _gen=gen: asyncio.create_task(
                        self._on_page_closed_if_current(sess, _gen)
                    ),
                )
            except Exception:
                pass
        if context is not None and not getattr(sess, "_context_listener_set", False):
            try:
                context.on(
                    "close",
                    lambda *_args: asyncio.create_task(
                        self._on_browser_disconnected(sess)
                    ),
                )
                sess._context_listener_set = True
            except Exception:
                pass
        if browser is not None and not getattr(sess, "_browser_listener_set", False):
            try:
                browser.on(
                    "disconnected",
                    lambda *_args: asyncio.create_task(
                        self._on_browser_disconnected(sess)
                    ),
                )
                sess._browser_listener_set = True
            except Exception:
                pass

    async def _on_page_closed_if_current(self, sess: KeptSiteSession, gen: int) -> None:
        if int(getattr(sess, "_listener_gen", 0) or 0) != gen:
            return
        await self._on_page_closed(sess)

    async def _on_page_closed(self, sess: KeptSiteSession) -> None:
        """单页关闭时禁止杀浏览器。

        进体育场馆时常会关掉大厅标签再开盘口标签；若此处 dispose，
        Chromium 会在刚进馆后被整窗关掉。
        """
        try:
            async with self._lock:
                cur = self._sessions.get(sess.key)
                if cur is not sess:
                    return  # 已被新会话替换

            context = sess.context
            alive: list = []
            if context is not None:
                try:
                    for p in list(context.pages):
                        try:
                            if p is not None and not p.is_closed():
                                alive.append(p)
                        except Exception:
                            continue
                except Exception:
                    alive = []

            if alive:
                from app.services.bookmakers.venue_entry import _is_venue_url

                chosen = alive[-1]
                for p in reversed(alive):
                    try:
                        u = p.url or ""
                    except Exception:
                        u = ""
                    if _is_venue_url(u):
                        chosen = p
                        break
                sess.page = chosen
                try:
                    sess.context = chosen.context
                except Exception:
                    pass
                try:
                    u = chosen.url or ""
                    if u:
                        sess.venue_url = u
                except Exception:
                    pass
                sess.last_refresh_at = time.time()
                self._setup_browser_listeners(sess)
                logger.info(
                    "page closed → switched to sibling tab key=%s site=%s url=%s",
                    sess.key,
                    sess.site_code or "?",
                    (sess.venue_url or "")[:120],
                )
                return

            # 暂无标签：不关浏览器（场馆可能马上再开新页）
            logger.warning(
                "page closed with no sibling tabs — keep browser open key=%s site=%s",
                sess.key,
                sess.site_code or "?",
            )
            sess.page = None
            # 短暂等待新标签出现后再决定是否软摘会话
            asyncio.create_task(self._recover_after_page_close(sess))
        except Exception:
            logger.exception("_on_page_closed failed key=%s", getattr(sess, "key", "?"))

    async def _recover_after_page_close(self, sess: KeptSiteSession) -> None:
        """进馆换页竞态：等新标签，仍无则软摘会话但不杀进程。"""
        try:
            await asyncio.sleep(1.5)
            async with self._lock:
                cur = self._sessions.get(sess.key)
                if cur is not sess:
                    return
            context = sess.context
            browser = sess.browser
            if context is not None:
                try:
                    for p in list(context.pages):
                        try:
                            if p is not None and not p.is_closed():
                                await self.update_page(sess.base_url, p)
                                logger.info(
                                    "recovered page after close key=%s",
                                    sess.key,
                                )
                                return
                        except Exception:
                            continue
                except Exception:
                    pass
            connected = False
            try:
                connected = bool(browser and browser.is_connected())
            except Exception:
                connected = False
            if connected:
                # 浏览器还在：保留会话壳，等下次 adopt/update；绝不 close
                logger.warning(
                    "no page after close; browser kept for re-adopt key=%s",
                    sess.key,
                )
                return
            # persistent context 关闭时没有 Browser.disconnected 事件；统一走
            # 完整断连逻辑，清本地凭据并通知后端将站点标成未连接。
            await self._on_browser_disconnected(sess)
        except Exception:
            logger.debug("recover_after_page_close ignored", exc_info=True)

    async def _on_browser_disconnected(self, sess: KeptSiteSession) -> None:
        """浏览器关闭时只清理所属实例，绝不误伤刚替换的新会话。"""
        if sess.suppress_disconnect_notify:
            logger.info(
                "intentional browser replacement closed key=%s site=%s",
                sess.key,
                sess.site_code or "?",
            )
            return
        logger.warning(
            "browser disconnected: key=%s site=%s",
            sess.key,
            sess.site_code or "?",
        )
        async with self._lock:
            if bool(getattr(sess, "_disconnect_handled", False)):
                return
            current = self._sessions.get(sess.key)
            same_runtime = bool(
                current is not None
                and (
                    (sess.browser is not None and current.browser is sess.browser)
                    or (sess.context is not None and current.context is sess.context)
                    or (sess.page is not None and current.page is sess.page)
                )
            )
            # 旧 Chromium 的 disconnected 事件可能晚于新浏览器 adopt。此时
            # key 已指向替代会话，旧回调必须完全忽略，不能清 token/通知后端。
            if current is not None and current is not sess and not same_runtime:
                logger.info(
                    "ignore stale browser disconnect key=%s old_site=%s new_site=%s",
                    sess.key,
                    sess.site_code or "?",
                    current.site_code or "?",
                )
                return

            sess._disconnect_handled = True

            doomed: list[KeptSiteSession] = []
            for k, s in list(self._sessions.items()):
                same_browser = sess.browser is not None and s.browser is sess.browser
                same_context = sess.context is not None and s.context is sess.context
                if s is sess or same_browser or same_context:
                    doomed.append(self._sessions.pop(k))
        for s in doomed:
            # browser 已断：只 stop playwright，勿再 browser.close
            try:
                pw = getattr(s, "playwright", None)
                if pw is not None:
                    await pw.stop()
            except Exception:
                pass
            s.page = None
            s.browser = None
            s.context = None
            s.playwright = None
            s.venue_url = ""
            s.token = ""
        # 浏览器事件处理完成前确认后端已收到断连通知，避免 UI 继续把已关闭
        # 的 OB/平博会话显示为“已连接”。
        try:
            await asyncio.wait_for(
                self._notify_backend_disconnected(sess),
                timeout=6.0,
            )
        except Exception as e:
            logger.debug("notify backend disconnected did not complete: %s", e)

    async def _notify_backend_disconnected(self, sess: KeptSiteSession) -> None:
        """通知后端该站浏览器已关，应标记 DISCONNECTED。"""
        import os

        import httpx

        base = (os.getenv("BOOKMAKER_BACKEND_URL") or "http://127.0.0.1:8000").rstrip("/")
        code = (sess.site_code or "").strip()
        if not code:
            return
        token = (os.getenv("INTERNAL_API_TOKEN") or "").strip()
        if not token:
            return
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(
                    f"{base}/api/v1/bookmakers/internal/browser-closed",
                    json={
                        "site_code": code,
                        "base_url": sess.base_url or "",
                    },
                    headers={"X-Internal-Token": token},
                )
                if resp.status_code >= 400:
                    logger.warning(
                        "notify backend browser-closed rejected status=%s body=%s",
                        resp.status_code,
                        (resp.text or "")[:160],
                    )
        except Exception as e:
            logger.debug("notify backend browser-closed failed: %s", e)

    async def update_page(self, base_url: str, page) -> None:
        """综合站进场馆后可能换到新标签页，更新长连接指向。"""
        sess = self.get(base_url) or self.find(base_url=base_url)
        if not sess or page is None:
            return
        try:
            if page.is_closed():
                return
        except Exception:
            return
        sess.page = page
        try:
            sess.context = page.context
        except Exception:
            pass
        try:
            u = page.url or ""
            if u:
                from app.services.bookmakers.venue_entry import _is_venue_url

                if _is_venue_url(u):
                    sess.venue_url = u
                elif not sess.venue_url:
                    sess.venue_url = u
        except Exception:
            pass
        sess.last_refresh_at = time.time()
        sess.hidden = False
        self._setup_browser_listeners(sess)

    async def probe_alive(
        self,
        base_url: str,
        *,
        session_token: str = "",
        touch_sport: bool = True,
        site_code: str = "ob",
        fast: bool = False,
    ) -> dict:
        """探测长连接是否仍可用（不弹窗）。fast=True：不导航。"""
        from app.services.bookmakers.plugins.ob.odds import sanitize_token
        from app.services.bookmakers.session_blob import apply_session_blob, is_session_blob
        from app.services.bookmakers.site_profiles import get_site_profile

        sess = self.find(base_url=base_url, site_code=site_code)
        if not sess or not sess.page or sess.page.is_closed():
            return {"ok": False, "token": "", "message": "无有效长连接"}

        token = sanitize_token(session_token) or sess.token or ""
        code = (site_code or sess.site_code or "ob").lower()
        profile = get_site_profile(code)
        try:
            if fast:
                if code == "ob" or profile.get("auth_mode") == "kaiyun":
                    tok = ""
                    try:
                        tok = await asyncio.wait_for(
                            sess.page.evaluate("() => localStorage.getItem('X-API-TOKEN') || ''"),
                            timeout=2.0,
                        )
                    except Exception:
                        tok = ""
                    if not tok:
                        tok = token if token and not is_session_blob(token) else token
                    if not tok:
                        return {"ok": False, "token": "", "message": "长连接会话已失效（无 Token）"}
                    sess.token = tok
                else:
                    if not (token or sess.token):
                        return {"ok": False, "token": "", "message": "长连接会话已失效"}
                    sess.token = token or sess.token
                sess.last_refresh_at = time.time()
                return {"ok": True, "token": sess.token, "message": "长连接可用（快检）"}

            if token:
                if is_session_blob(token):
                    await apply_session_blob(sess.context, sess.page, token)
                else:
                    await sess.page.evaluate(
                        "(t) => { try { localStorage.setItem('X-API-TOKEN', t); } catch (e) {} }",
                        token,
                    )
                sess.token = token
            if touch_sport:
                # 禁止 goto 恢复：写死/陈旧 venue_url 会闪退并踢登录
                # 仅在当前页已是盘口时，动态记下 URL
                try:
                    from app.services.bookmakers.venue_entry import capture_live_venue_url

                    live = await capture_live_venue_url(sess.page)
                    if live:
                        sess.venue_url = live
                except Exception as e:
                    logger.debug("probe_alive capture venue skipped: %s", e)
            else:
                try:
                    await asyncio.wait_for(sess.page.evaluate("() => 1"), timeout=2.0)
                except Exception as e:
                    return {"ok": False, "token": "", "message": f"长连接页无响应: {e}"}

            if code == "ob" or profile.get("auth_mode") == "kaiyun":
                tok = await sess.page.evaluate("() => localStorage.getItem('X-API-TOKEN') || ''")
                if not tok and token and not is_session_blob(token):
                    await sess.page.evaluate(
                        "(t) => { try { localStorage.setItem('X-API-TOKEN', t); } catch (e) {} }",
                        token,
                    )
                    tok = token
                if not tok and is_session_blob(token):
                    tok = token
                if not tok:
                    return {"ok": False, "token": "", "message": "长连接会话已失效（无 Token）"}
                sess.token = tok
            else:
                tok = token or sess.token
                if not tok:
                    return {"ok": False, "token": "", "message": "长连接会话已失效"}
                sess.token = tok

            sess.last_refresh_at = time.time()
            return {"ok": True, "token": sess.token, "message": "长连接可用"}
        except Exception as e:
            logger.warning("probe_alive failed: %s", e)
            return {"ok": False, "token": "", "message": f"长连接探测失败: {e}"}

    async def invalidate(self, base_url: str) -> None:
        await self.close(base_url)

    async def ensure_for_odds(
        self,
        *,
        base_url: str,
        session_token: str,
        recreate: bool = False,
        site_code: str = "ob",
        venue_url: str = "",
    ) -> Optional[KeptSiteSession]:
        """
        只复用已有登录长连接，禁止再 launch 新 Chromium。
        （新窗口一律由 /login 验证流程创建，避免一站多窗）
        """
        from app.services.bookmakers.browser_login import apply_desktop_viewport
        from app.services.bookmakers.plugins.ob.odds import sanitize_token
        from app.services.bookmakers.venue_entry import _is_dead_page_url, _is_venue_url, is_in_sportsbook

        token = sanitize_token(session_token)
        base = (base_url or "").rstrip("/")
        if not base:
            return None
        code = (site_code or "ob").lower()
        want_venue = (venue_url or "").strip()
        key = _session_key(base)

        if recreate:
            logger.info("ensure_for_odds ignore recreate (single-browser policy) site=%s", code)

        sess = self.find(base_url=base, site_code=code)
        # base_url 命中但 page 已关：再按 site_code 找；都没有才报无会话
        if sess is None:
            raw = self.get(base) if base else None
            if raw is not None and not self._page_alive(raw):
                logger.warning(
                    "ensure_for_odds %s: session key=%s page closed — drop zombie",
                    code,
                    key,
                )
                self._sessions.pop(key, None)
                sess = self.get_by_site_code(code)
        if sess and self._page_alive(sess):
            try:
                cur_u = sess.page.url or ""
            except Exception:
                cur_u = ""

            if _is_dead_page_url(cur_u):
                # 检查页面是否真的死掉（不仅 URL 看起来是死的）
                try:
                    await asyncio.wait_for(
                        sess.page.evaluate("() => document.readyState"),
                        timeout=2.0,
                    )
                except Exception:
                    logger.warning("ensure_for_odds %s: page truly dead url=%s, invalidate", code, cur_u[:80])
                    await self.invalidate(sess.base_url or base)
                    return None
                # 页面还活着，只是 URL 看起来死 → 可能是 SPA 路由
                logger.info("ensure_for_odds %s: url looks dead but page alive=%s — keep session", code, cur_u[:80])

            if want_venue and _is_venue_url(want_venue):
                sess.venue_url = sess.venue_url or want_venue
            if token:
                sess.token = token
            sess.site_code = code or sess.site_code

            probe = await self.probe_alive(
                sess.base_url or base,
                session_token=token or sess.token,
                touch_sport=False,
                site_code=code,
                fast=True,
            )
            if not probe.get("ok"):
                logger.warning("ensure_for_odds %s: probe failed — reuse page anyway", code)

            try:
                in_book = await is_in_sportsbook(sess.page)
            except Exception:
                in_book = False

            # 禁止 goto 旧 venue_url（场馆地址会变，硬恢复会闪退/掉登录）
            # 仅从当前页动态捕获
            try:
                from app.services.bookmakers.venue_entry import capture_live_venue_url

                live = await capture_live_venue_url(sess.page)
                if live:
                    sess.venue_url = live
                elif in_book:
                    try:
                        cu = sess.page.url or ""
                        if cu:
                            sess.venue_url = cu
                    except Exception:
                        pass
            except Exception:
                pass

            try:
                await apply_desktop_viewport(sess.page)
            except Exception:
                pass

            # 仅清理「不同浏览器」的同站僵尸会话；绝不 dispose 当前窗
            async with self._lock:
                for k, other in list(self._sessions.items()):
                    if other is sess:
                        continue
                    same_slot = k == key or (code and (other.site_code or "").lower() == code)
                    if not same_slot:
                        continue
                    if other.browser is sess.browser or other.page is sess.page:
                        self._sessions.pop(k, None)
                        continue
                    self._sessions.pop(k, None)
                    try:
                        other.suppress_disconnect_notify = True
                        await self._dispose(other)
                    except Exception:
                        pass
                sess.key = key
                self._sessions[key] = sess
            return sess

        raw = self.get(base) if base else None
        logger.warning(
            "ensure_for_odds %s: no kept session key=%s keys=%s raw_page_closed=%s — re-verify required (no new browser)",
            code,
            key,
            list(self._sessions.keys()),
            (not self._page_alive(raw)) if raw is not None else None,
        )
        return None

    async def refresh(self, base_url: str, *, force: bool = False, site_code: str = "") -> bool:
        """软保活：只探测存活 + 动态记下当前场馆 URL。绝对禁止 page.goto / reload。"""
        from app.services.bookmakers.venue_entry import capture_live_venue_url, is_in_sportsbook

        _ = force  # 保留签名；force 也不再导航
        sess = self.find(base_url=base_url, site_code=site_code)
        if not sess or not sess.page or sess.page.is_closed():
            return False
        try:
            await asyncio.wait_for(sess.page.evaluate("() => document.readyState"), timeout=2.5)
        except Exception as e:
            logger.warning("refresh page not responsive key=%s: %s", sess.key, e)
            return False

        try:
            live = await capture_live_venue_url(sess.page)
            if live:
                sess.venue_url = live
            else:
                try:
                    if await is_in_sportsbook(sess.page):
                        cu = sess.page.url or ""
                        if cu:
                            sess.venue_url = cu
                except Exception:
                    pass
        except Exception:
            pass

        sess.hidden = False
        sess.last_refresh_at = time.time()
        return True


site_sessions = SiteSessionManager()
