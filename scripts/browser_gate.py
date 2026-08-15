#!/usr/bin/env python3
"""
浏览器网关：每站点独立车道（独立锁 + 独立长连接浏览器）。

在 Mac/GUI 宿主机运行，弹出可见 Chromium；后端经 host.docker.internal:9277 调用。
OB 滚球同步不再挡住平博验证。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    force=True,
)
logger = logging.getLogger("browser-gate")
logging.getLogger("app.services.bookmakers").setLevel(logging.INFO)

# macOS 本机 / Linux 宿主机 / Docker 容器 统一缓存路径
if sys.platform == "darwin":
    _stable_pw = os.path.expanduser("~/Library/Caches/ms-playwright")
else:
    _stable_pw = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "/ms-playwright"
    if not Path(_stable_pw).exists():
        _stable_pw = os.path.expanduser("~/.cache/ms-playwright")
_cur = (os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or "").strip()
if (not _cur) or ("cursor-sandbox-cache" in _cur) or (not Path(_cur).exists()):
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = _stable_pw
Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"]).mkdir(parents=True, exist_ok=True)
# 本机 Gate 勿走代理，避免健康检查/拉浏览器失败
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(_k, None)
os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 登录弹窗前先修好 Crashpad 可写目录 / HOME（避免验证时 Chromium SIGABRT）
try:
    from app.services.bookmakers.browser_runtime import prepare_chromium_env

    prepare_chromium_env()
except Exception as _e:
    logger.warning("prepare_chromium_env skipped: %s", _e)

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
import uvicorn
from contextlib import asynccontextmanager

from app.services.bookmakers.browser_login import interactive_site_login
from app.services.bookmakers.plugins.ob.odds import sanitize_token
from app.services.bookmakers.plugin import get_plugin
from app.services.bookmakers.site_odds import fetch_site_odds_via_page
from app.services.bookmakers.site_session import site_sessions

_KEEP_REFRESH_SEC = 45  # 拉长保活间隔，少与盘口同步抢车道
_refresh_task: asyncio.Task | None = None

# ── 平博下单接口抓包（一次性任务：挂监听后由用户手动完成一次真实下单）──
_BET_CAPTURE_LOG = "/tmp/pinnacle_bet_capture.log"
_bet_capture_hooked: set[int] = set()  # 已挂钩的 page hash，防重复


def _hook_pinnacle_bet_capture(page) -> None:
    """给平博长连接页挂全量 XHR 监听，捕获下单接口端点/payload/回执。

    只记 POST（下单必然 POST），过滤静态资源；写 /tmp/pinnacle_bet_capture.log。
    成功捕获 bet 相关端点后可移除此钩子。
    """
    if page is None or page.is_closed():
        return
    key = id(page)
    if key in _bet_capture_hooked:
        return
    _bet_capture_hooked.add(key)

    def _on_response(resp):
        try:
            req = resp.request
            if req.method.upper() != "POST":
                return
            url = req.url or ""
            # 过滤明显非业务请求
            low = url.lower()
            if any(x in low for x in (".js", ".css", ".png", ".jpg", ".woff", "analytics", "beacon", "log")):
                return
            body = req.post_data or ""
            with open(_BET_CAPTURE_LOG, "a", encoding="utf-8") as f:
                ts = __import__("time").strftime("%F %T")
                f.write(f"\n[{ts}] POST {url}\n")
                f.write(f"  status={resp.status}\n")
                if body:
                    f.write(f"  req_body={body[:2000]}\n")
                try:
                    # 同步读取响应体（小响应可接受）
                    rb = resp.text()[:2000] if resp.status < 400 else ""
                    if rb:
                        f.write(f"  resp_body={rb}\n")
                except Exception:
                    pass
        except Exception:
            pass

    try:
        page.on("response", _on_response)
    except Exception:
        pass


async def _keep_sessions_refresh_loop() -> None:
    """
    长连接保活：只刮余额 + 掉线时软恢复 venue。
    禁止周期性 page.reload（会导致可见窗口不停跳转闪烁并打崩 SPA）。
    """
    await asyncio.sleep(8)
    while True:
        try:
            from app.services.bookmakers.catalog import BOOKMAKER_CATALOG
            from app.services.bookmakers.venue_entry import is_in_sportsbook

            host_to_code = {}
            for code, meta in BOOKMAKER_CATALOG.items():
                try:
                    h = (urlparse(str(meta.get("default_url") or "")).hostname or "").lower()
                    if h:
                        host_to_code[h] = code
                        if h.startswith("www."):
                            host_to_code[h[4:]] = code
                        else:
                            host_to_code[f"www.{h}"] = code
                except Exception:
                    continue

            items = list((getattr(site_sessions, "_sessions", {}) or {}).values())
            for sess in items:
                if not sess or not sess.page or sess.page.is_closed():
                    continue
                code = (sess.site_code or "").lower()
                if not code:
                    try:
                        h = (urlparse(sess.base_url or "").hostname or "").lower()
                        code = host_to_code.get(h) or ""
                        if code:
                            sess.site_code = code
                    except Exception:
                        pass
                base = sess.base_url or ""
                try:
                    # 平博页挂下单接口抓包（一次性任务，见 _hook_pinnacle_bet_capture）
                    if code == "pinnacle" and sess.page and not sess.page.is_closed():
                        _hook_pinnacle_bet_capture(sess.page)
                    # 绝对禁止 goto/reload：只探测存活 + 刮余额 + 动态记下当前 URL
                    ok = await site_sessions.refresh(base, force=False, site_code=code)
                    if not ok:
                        logger.warning("keep-alive page not responsive site=%s", code or base[:40])
                        continue
                    if sess.page and not sess.page.is_closed():
                        try:
                            in_book = await is_in_sportsbook(sess.page)
                        except Exception:
                            in_book = False
                        # 仅场馆内刷新余额；大厅/中心钱包不写入
                        if in_book:
                            bal, recognized = await _scrape_balance_from_page(
                                sess.page,
                                site_code=code,
                                session_token=getattr(sess, "token", "") or "",
                            )
                            if recognized:
                                sess.last_balance = float(bal)
                                sess.balance_recognized = True
                        logger.info(
                            "keep-alive site=%s in_book=%s venue_bal=%.4f url=%s",
                            code or "?",
                            in_book,
                            float(sess.last_balance or 0),
                            (getattr(sess.page, "url", "") or "")[:80],
                        )
                except Exception:
                    logger.exception("keep-alive failed site=%s", code or base[:40])
        except Exception:
            logger.exception("keep-alive loop error")
        await asyncio.sleep(_KEEP_REFRESH_SEC)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    global _refresh_task
    _refresh_task = asyncio.create_task(_keep_sessions_refresh_loop(), name="keep-alive-soft")
    logger.info("browser keep-alive started (soft, every %ss, no reload)", _KEEP_REFRESH_SEC)
    try:
        yield
    finally:
        if _refresh_task and not _refresh_task.done():
            _refresh_task.cancel()
        _refresh_task = None


app = FastAPI(title="Sports Browser Gate", version="1.7.0", lifespan=_lifespan)

# 内部鉴权：配置 INTERNAL_API_TOKEN 后，除 /health 外均需 X-Internal-Token
_GATE_TOKEN = (os.getenv("INTERNAL_API_TOKEN") or "").strip()
_GATE_AUTH_REQUIRED = (os.getenv("GATE_REQUIRE_AUTH") or "").strip().lower() in (
    "1",
    "true",
    "yes",
) or bool(_GATE_TOKEN)


@app.middleware("http")
async def _gate_internal_auth(request: Request, call_next):
    path = request.url.path or ""
    if path in ("/health", "/", "/docs", "/openapi.json", "/redoc"):
        return await call_next(request)
    if not _GATE_AUTH_REQUIRED:
        return await call_next(request)
    got = (request.headers.get("x-internal-token") or "").strip()
    if not _GATE_TOKEN or got != _GATE_TOKEN:
        return JSONResponse(
            status_code=401,
            content={"ok": False, "message": "unauthorized: missing or invalid X-Internal-Token"},
        )
    return await call_next(request)


def _lane_key(base_url: str = "", site_code: str = "") -> str:
    """每站独立车道：不同站点互不抢锁。"""
    raw = (base_url or "").strip().rstrip("/")
    if raw:
        try:
            u = urlparse(raw if "://" in raw else f"https://{raw}")
            host = (u.hostname or raw).lower()
            if host.startswith("www."):
                host = host[4:]
            port = u.port or (443 if (u.scheme or "https") == "https" else 80)
            return f"{host}:{port}"
        except Exception:
            fallback = raw.lower()
            if fallback.startswith("www."):
                fallback = fallback[4:]
            return fallback
    return (site_code or "default").lower()


class _SiteLane:
    __slots__ = ("key", "lock", "busy_op", "login_priority_until", "bet_priority_until")

    def __init__(self, key: str):
        self.key = key
        self.lock = asyncio.Lock()
        self.busy_op: str | None = None
        self.login_priority_until: float = 0.0
        self.bet_priority_until: float = 0.0

    def want_login(self) -> bool:
        return time.time() < float(self.login_priority_until or 0)

    def set_login_priority(self, seconds: float = 45.0) -> None:
        self.login_priority_until = time.time() + max(5.0, float(seconds))

    def clear_login_priority(self) -> None:
        self.login_priority_until = 0.0

    def want_bet(self) -> bool:
        return time.time() < float(self.bet_priority_until or 0)

    def set_bet_priority(self, seconds: float = 40.0) -> None:
        self.bet_priority_until = time.time() + max(5.0, float(seconds))

    def clear_bet_priority(self) -> None:
        self.bet_priority_until = 0.0


_lanes: dict[str, _SiteLane] = {}
_lanes_guard = asyncio.Lock()


async def _get_lane(base_url: str = "", site_code: str = "") -> _SiteLane:
    key = _lane_key(base_url, site_code)
    async with _lanes_guard:
        lane = _lanes.get(key)
        if lane is None:
            lane = _SiteLane(key)
            _lanes[key] = lane
        return lane


def _lanes_snapshot() -> dict:
    return {
        k: {"busy": v.lock.locked(), "busy_op": v.busy_op, "login_priority": v.want_login()}
        for k, v in _lanes.items()
    }


class LoginRequest(BaseModel):
    base_url: str
    username: str = ""
    password: str = ""
    session_token: str = ""
    # 人工验证码可能需要较长时间；与浏览器登录层的 5 分钟上限保持一致。
    wait_seconds: int = Field(default=35, ge=10, le=300)
    force_new: bool = False
    site_code: str = "ob"
    manual_venue: bool = False


class PreferLoginRequest(BaseModel):
    seconds: float = Field(default=45, ge=5, le=120)
    base_url: str = ""
    site_code: str = "ob"


class OddsSyncRequest(BaseModel):
    base_url: str
    session_token: str
    limit: int = Field(default=800, ge=10, le=1500)
    headed: bool = False
    live_only: bool = False
    site_code: str = "ob"
    venue_url: str = ""


class BetPlaceRequest(BaseModel):
    base_url: str
    session_token: str
    match_external_id: str
    selection: str
    odds: float
    stake: float
    bet_type: str = "moneyline"
    odds_data: dict = Field(default_factory=dict)
    headed: bool = False
    site_code: str = "ob"


class BetHistoryRequest(BaseModel):
    base_url: str = ""
    session_token: str = ""
    site_code: str = "ob"
    days: int = 3


@app.get("/health")
async def health():
    kept = 0
    try:
        kept = len(getattr(site_sessions, "_sessions", {}) or {})
    except Exception:
        kept = 0
    lanes = _lanes_snapshot()
    any_busy = any(v.get("busy") for v in lanes.values())
    busy_ops = [f"{k}:{v.get('busy_op')}" for k, v in lanes.items() if v.get("busy_op")]
    # 仅在 Mac/GUI 宿主机运行，runtime 固定为 host
    runtime = "host"
    return {
        "ok": True,
        "service": "browser-gate",
        "version": "1.9.0",
        "runtime": runtime,
        "headed": True,
        "browsers_path": (os.getenv("PLAYWRIGHT_BROWSERS_PATH") or "")[:120],
        "mode": "per-site-lanes",
        "busy": any_busy,
        "busy_op": busy_ops[0] if busy_ops else None,
        "busy_ops": busy_ops,
        "lanes": lanes,
        "kept_sessions": kept,
        "login_priority": any(v.get("login_priority") for v in lanes.values()),
        "keep_refresh_sec": _KEEP_REFRESH_SEC,
        "session_balances": [
            {
                "site_code": getattr(s, "site_code", "") or "",
                "balance": float(getattr(s, "last_balance", 0) or 0),
                "key": getattr(s, "key", "") or "",
            }
            for s in list((getattr(site_sessions, "_sessions", {}) or {}).values())
        ],
        "browser_instances": site_sessions.browser_count(),
        "session_keys": list((getattr(site_sessions, "_sessions", {}) or {}).keys()),
    }


@app.post("/prefer-login")
async def prefer_login(req: PreferLoginRequest):
    """验证前调用：仅让同一站点的盘口同步让路，其它站继续跑。"""
    lane = await _get_lane(req.base_url, req.site_code)
    lane.set_login_priority(req.seconds)
    return {
        "ok": True,
        "lane": lane.key,
        "until": lane.login_priority_until,
        "busy": lane.lock.locked(),
        "busy_op": lane.busy_op,
    }


@app.post("/login")
async def login(req: LoginRequest):
    site_code = (req.site_code or "ob").lower()
    token_hint = sanitize_token(req.session_token)
    lane = await _get_lane(req.base_url, site_code)
    lane.set_login_priority(50)

    # 快路径：已有长连接（不占锁）——综合站须已在盘口页才直接复用
    if not req.force_new:
        existing = site_sessions.find(base_url=req.base_url, site_code=site_code)
        if existing and existing.page and not existing.page.is_closed():
            can_reuse = True
            if site_code in ("ob", "pinnacle"):
                try:
                    from app.services.bookmakers.venue_entry import is_in_sportsbook

                    can_reuse = await is_in_sportsbook(existing.page)
                except Exception:
                    can_reuse = False
            if can_reuse:
                probe = await site_sessions.probe_alive(
                    existing.base_url or req.base_url,
                    session_token=token_hint or existing.token,
                    touch_sport=False,
                    site_code=site_code,
                    fast=True,
                )
                if probe.get("ok"):
                    # 复用成功立即挂钩抓包（平博）
                    if site_code == "pinnacle":
                        try:
                            _hook_pinnacle_bet_capture(existing.page)
                            logger.info("pinnacle bet-capture hooked at login-reuse (pid=%s)", id(existing.page))
                        except Exception:
                            pass
                    try:
                        vu = existing.venue_url or (existing.page.url or "")
                    except Exception:
                        vu = existing.venue_url or ""
                    profile = {"name": req.username} if req.username else {}
                    if vu:
                        profile["venue_url"] = vu
                    bal = 0.0
                    try:
                        bal, recognized = await _scrape_balance_from_page(
                            existing.page,
                            site_code=site_code,
                            session_token=getattr(existing, "token", "") or req.session_token or "",
                        )
                        if recognized:
                            profile["balance"] = bal
                            existing.last_balance = float(bal)
                            existing.balance_recognized = True
                            existing.site_code = site_code or existing.site_code
                    except Exception:
                        pass
                    lane.clear_login_priority()
                    return {
                        "ok": True,
                        "message": "已复用长连接（已在体育场馆），未再次弹窗",
                        "token": probe.get("token") or existing.token or token_hint,
                        "profile": profile,
                        "balance": bal,
                        "kept_alive": True,
                        "reused": True,
                        "lane": lane.key,
                    }
            # 未在盘口：禁止 invalidate 再开第二窗；交由 interactive_site_login 复用同窗
            logger.info(
                "login reuse existing window (not in sportsbook) site=%s key=%s",
                site_code,
                getattr(existing, "key", ""),
            )

    # 只等本站锁：其它站 odds-sync-live 不再挡住验证
    deadline = time.time() + 6.0
    acquired = False
    while time.time() < deadline:
        try:
            await asyncio.wait_for(lane.lock.acquire(), timeout=0.35)
            acquired = True
            break
        except asyncio.TimeoutError:
            continue
    if not acquired:
        lane.clear_login_priority()
        return {
            "ok": False,
            "busy": True,
            "message": f"本站浏览器正忙（{lane.busy_op or '占用中'}），请 2 秒后再点验证",
            "token": "",
            "profile": {},
            "balance": 0,
            "lane": lane.key,
        }

    lane.busy_op = "login"
    try:
        result = await interactive_site_login(
            base_url=req.base_url,
            username=req.username,
            password=req.password,
            session_token=token_hint,
            wait_seconds=min(int(req.wait_seconds or 90), 120),
            nav_timeout_ms=20000,
            keep_alive=True,
            force_new=bool(req.force_new),
            site_code=site_code,
            manual_venue=bool(req.manual_venue),
        )
        if isinstance(result, dict):
            result["lane"] = lane.key
        # 登录完成立即挂钩抓包（平博）：不等 45s keep-alive，会话一就绪就监听
        if site_code == "pinnacle" and isinstance(result, dict) and result.get("ok"):
            try:
                sess = site_sessions.find(base_url=req.base_url, site_code="pinnacle")
                if sess and sess.page and not sess.page.is_closed():
                    _hook_pinnacle_bet_capture(sess.page)
                    logger.info("pinnacle bet-capture hooked at login (pid=%s)", id(sess.page))
            except Exception:
                pass
        return result
    except Exception as e:
        return {
            "ok": False,
            "message": " ".join(str(f"浏览器登录异常: {e}").split()),
            "token": "",
            "profile": {},
            "balance": 0,
            "lane": lane.key,
        }
    finally:
        lane.busy_op = None
        lane.clear_login_priority()
        lane.lock.release()


async def _run_odds_sync(req: OddsSyncRequest):
    site_code = (req.site_code or "ob").lower()
    token = sanitize_token(req.session_token)
    if not token:
        return {"ok": False, "message": "缺少 session token", "matches": []}

    lane = await _get_lane(req.base_url, site_code)
    if lane.want_login() or lane.want_bet():
        return {
            "ok": False,
            "busy": True,
            "message": "本站验证/下单优先，跳过本次盘口拉取",
            "matches": [],
            "lane": lane.key,
        }

    # 滚球同步：等同一站其它 odds/balance 让出锁（勿立刻 busy）
    lock_wait = 35.0 if req.live_only else 8.0
    acquired = False
    deadline = time.time() + lock_wait
    while time.time() < deadline:
        if lane.want_login() or lane.want_bet():
            return {
                "ok": False,
                "busy": True,
                "message": "本站验证/下单优先，跳过本次盘口拉取",
                "matches": [],
                "lane": lane.key,
            }
        try:
            await asyncio.wait_for(lane.lock.acquire(), timeout=0.4)
            acquired = True
            break
        except asyncio.TimeoutError:
            continue
    if not acquired:
        return {
            "ok": False,
            "busy": True,
            "message": f"本站浏览器正忙（{lane.busy_op or '占用中'}），跳过本次盘口拉取",
            "matches": [],
            "lane": lane.key,
        }
    if lane.want_login() or lane.want_bet():
        lane.lock.release()
        return {
            "ok": False,
            "busy": True,
            "message": "本站验证/下单优先，跳过本次盘口拉取",
            "matches": [],
            "lane": lane.key,
        }

    lane.busy_op = "odds-sync-live" if req.live_only else "odds-sync"
    refreshed = False
    sess = None
    matches: list = []
    # 双球类已加速/冷却后，滚球超时可收紧
    odds_timeout = 70.0 if req.live_only else 75.0
    try:
        async def _fetch_with_session(page):
            if lane.want_login() or lane.want_bet():
                return []
            plug = get_plugin(site_code)
            # 站点专属盘口（OB YBTY）；返回 None 则走通用页面采集
            try:
                rows = await plug.fetch_live_odds(
                    page,
                    base_url=req.base_url,
                    session_token=token,
                    limit=req.limit,
                    live_only=bool(req.live_only),
                    venue_url=(req.venue_url or "").strip(),
                )
                if rows:
                    return rows
            except Exception as e:
                logger.warning("%s plugin odds failed, fallback venue-live: %s", site_code, e)
            if page is None:
                return []
            return await fetch_site_odds_via_page(
                page,
                site_code=site_code,
                base_url=req.base_url,
                limit=req.limit,
                live_only=bool(req.live_only),
                wait_ms=2200 if req.live_only else 5000,
            )

        if lane.want_login() or lane.want_bet():
            return {
                "ok": False,
                "busy": True,
                "message": "本站验证/下单优先，跳过本次盘口拉取",
                "matches": [],
                "lane": lane.key,
            }

        sess = await site_sessions.ensure_for_odds(
            base_url=req.base_url,
            session_token=token,
            site_code=site_code,
            venue_url=(req.venue_url or "").strip(),
        )
        if lane.want_login() or lane.want_bet():
            return {
                "ok": False,
                "busy": True,
                "message": "本站验证优先，跳过本次盘口拉取",
                "matches": [],
                "lane": lane.key,
            }
        if not sess:
            from app.services.bookmakers.site_profiles import needs_manual_venue

            if needs_manual_venue(site_code):
                return {
                    "ok": False,
                    "message": "无有效盘口长连接。请在站点配置重新验证并手动进入场馆后再同步",
                    "matches": [],
                    "refreshed": False,
                    "lane": lane.key,
                }

        if sess:
            # 软保活：已在盘口不 reload；掉线才恢复 venue（禁止 force 硬刷新闪烁）
            refreshed = await site_sessions.refresh(req.base_url, force=False, site_code=site_code)

        matches = await asyncio.wait_for(
            _fetch_with_session(sess.page if sess else None),
            timeout=odds_timeout,
        )
        # 手动场馆站：禁止 recreate（会毁掉已进盘口页并跳到大厅）
        from app.services.bookmakers.site_profiles import needs_manual_venue

        # 站点插件空结果恢复（平博回滚球）；无插件恢复则 recreate / soft 重试
        if (not matches) and sess:
            try:
                recovered = await get_plugin(site_code).after_empty_odds(sess.page)
            except Exception as e:
                logger.warning("%s empty recover failed: %s", site_code, e)
                recovered = False
            if recovered:
                try:
                    matches = await asyncio.wait_for(
                        _fetch_with_session(sess.page),
                        timeout=odds_timeout,
                    )
                except Exception as e:
                    logger.warning("%s re-fetch after recover failed: %s", site_code, e)
            elif not lane.want_login() and not lane.want_bet() and not needs_manual_venue(site_code):
                sess = await site_sessions.ensure_for_odds(
                    base_url=req.base_url,
                    session_token=token,
                    recreate=True,
                    site_code=site_code,
                    venue_url=(req.venue_url or "").strip(),
                )
                if sess:
                    refreshed = (
                        await site_sessions.refresh(
                            req.base_url, force=False, site_code=site_code
                        )
                        or refreshed
                    )
                matches = await asyncio.wait_for(
                    _fetch_with_session(sess.page if sess else None),
                    timeout=odds_timeout,
                )
            else:
                # 空结果重试：禁止 goto。已在滚球盘则静默再采；否则 gentle 点一次 Tab。
                try:
                    from app.services.bookmakers.venue_entry import (
                        activate_sportsbook_tabs,
                        capture_live_venue_url,
                        page_already_on_live_board,
                    )

                    try:
                        live = await capture_live_venue_url(sess.page)
                        if live:
                            sess.venue_url = live
                    except Exception:
                        pass
                    on_live = False
                    try:
                        on_live = await page_already_on_live_board(sess.page)
                    except Exception:
                        on_live = False
                    if not on_live:
                        try:
                            await activate_sportsbook_tabs(
                                sess.page,
                                live_only=bool(req.live_only),
                                gentle=True,
                            )
                        except Exception:
                            pass
                    matches = await asyncio.wait_for(
                        _fetch_with_session(sess.page),
                        timeout=odds_timeout,
                    )
                except Exception:
                    pass
    except asyncio.TimeoutError:
        return {
            "ok": False,
            "message": "盘口拉取超时，请稍后重试",
            "matches": [],
            "refreshed": refreshed,
            "lane": lane.key,
        }
    except Exception as e:
        if lane.want_login() or lane.want_bet():
            return {
                "ok": False,
                "busy": True,
                "message": "本站验证/下单优先，跳过本次盘口拉取",
                "matches": [],
                "lane": lane.key,
            }
        try:
            from app.services.bookmakers.site_profiles import needs_manual_venue

            sess = await site_sessions.ensure_for_odds(
                base_url=req.base_url,
                session_token=token,
                recreate=not needs_manual_venue(site_code),
                site_code=site_code,
                venue_url=(req.venue_url or "").strip(),
            )
            matches = await asyncio.wait_for(
                _fetch_with_session(sess.page if sess else None),
                timeout=odds_timeout,
            )
        except Exception:
            return {
                "ok": False,
                "message": f"盘口拉取失败: {e}",
                "matches": [],
                "refreshed": refreshed,
                "lane": lane.key,
            }
    finally:
        lane.busy_op = None
        lane.lock.release()

    # 产品范围：只返回已开赛的足球/篮球；剔除球类矛盾与未开赛 LIVE
    from app.services.bookmakers.match_live import remote_match_started
    from app.services.bookmakers.sport_classify import normalize_sport, reject_sport_mismatch

    raw_n = len(matches or [])
    try:
        page_url = ""
        if sess and getattr(sess, "page", None) and not sess.page.is_closed():
            page_url = (sess.page.url or "")[:160]
    except Exception:
        page_url = ""
    filtered = []
    dropped_status = 0
    dropped_started = 0
    for m in matches or []:
        sport = normalize_sport(getattr(m, "sport", None))
        if sport not in ("football", "basketball"):
            continue
        if reject_sport_mismatch(
            sport,
            period=str(getattr(m, "period", "") or ""),
            home_score=getattr(m, "home_score", 0) or 0,
            away_score=getattr(m, "away_score", 0) or 0,
            text=f"{getattr(m, 'league', '')} {getattr(m, 'home_team', '')} {getattr(m, 'away_team', '')}",
        ):
            continue
        try:
            m.sport = sport
        except Exception:
            pass
        status = str(getattr(m, "status", "") or "").strip().lower()
        # 产品范围：永远只返回滚球足球/篮球（忽略今日/早盘）
        if status not in ("live", "inplay", "in_play", "running", "started"):
            dropped_status += 1
            continue
        if not remote_match_started(m):
            dropped_started += 1
            continue
        try:
            m.status = "live"
        except Exception:
            pass
        filtered.append(m)
    matches = filtered
    logger.info(
        "odds-sync %s raw=%d out=%d drop_status=%d drop_started=%d url=%s",
        site_code,
        raw_n,
        len(matches),
        dropped_status,
        dropped_started,
        page_url,
    )

    payload = []
    for m in matches:
        payload.append(
            {
                "external_id": m.external_id,
                "sport": m.sport,
                "league": m.league,
                "home_team": m.home_team,
                "away_team": m.away_team,
                "start_time": m.start_time,
                "status": m.status,
                "venue": m.venue,
                "home_score": int(m.home_score or 0),
                "away_score": int(m.away_score or 0),
                "clock": m.clock or "",
                "period": m.period or "",
                "odds_list": [
                    {
                        "bet_type": o.bet_type,
                        "odds_data": o.odds_data,
                        "spread": o.spread,
                        "total": o.total,
                    }
                    for o in (m.odds_list or [])
                ],
            }
        )
    return {
        "ok": True,
        "message": f"已拉取 {len(payload)} 场{'滚球' if req.live_only else '真实盘口'}"
        + ("（已刷新隐藏站）" if refreshed else ""),
        "matches": payload,
        "raw_count": raw_n,
        "page_url": page_url,
        "live_only": bool(req.live_only),
        "refreshed": refreshed,
        "kept_alive": bool(sess),
        "site_code": site_code,
        "lane": lane.key,
    }


class SessionCloseRequest(BaseModel):
    base_url: str = ""
    site_code: str = "ob"


class BalanceRequest(BaseModel):
    base_url: str
    session_token: str = ""
    site_code: str = "ob"


def _is_portal_sport_shell(url: str) -> bool:
    """综合站体育入口壳（非 OB 真盘口 H5）。"""
    u = (url or "").lower().strip()
    if not u:
        return False
    path = u.split("?")[0]
    if re.search(r"/game/sport(/[^/?#]+)?/?$", path):
        if not re.search(r"/game/sport/[^/?#]+/[^/?#]+", path):
            return True
    if re.search(r"[?&]enname=ybty\b", u) and "token=" not in u and "app-h5" not in u and "yewu" not in u:
        return True
    return False


async def _fetch_balance_via_page_api(page, *, site_code: str = "", session_token: str = "") -> float:
    """在体育场馆页上下文中读场馆钱包；优先 sportBalance，拒绝中心钱包。"""
    code = (site_code or "").lower()
    if code not in ("ob", ""):
        return 0.0
    api_token = ""
    raw = (session_token or "").strip()
    if raw and not raw.startswith("sess:"):
        api_token = raw

    # OB：优先在真盘口 iframe / H5（含 sport token）里取可投余额；禁止综合站壳上的中心/可转金额
    if code == "ob":
        ob_js = """async () => {
          const num = (v) => {
            const n = Number(v);
            return (!isNaN(n) && n >= 0 && n < 50000000) ? n : null;
          };
          const pick = (obj, depth) => {
            if (!obj || depth > 6 || typeof obj !== 'object') return null;
            // 勿把通用 balance 放前面：综合站/个人中心常是中心钱包
            for (const k of [
              'sportBalance','venueBalance','tyBalance','ybtyBalance','sportWalletBalance',
              'sportsBalance','venueWalletBalance','betBalance'
            ]) {
              if (obj[k] != null) {
                const n = num(obj[k]);
                if (n != null) return n;
              }
            }
            for (const nest of ['data','result','user','userInfo','wallet','finance','member']) {
              if (obj[nest] && typeof obj[nest] === 'object' && nest !== 'centerWallet') {
                const v = pick(obj[nest], depth + 1);
                if (v != null) return v;
              }
            }
            return null;
          };
          const qs = new URLSearchParams(location.search || '');
          let sportToken = qs.get('token') || '';
          try {
            sportToken = sportToken
              || localStorage.getItem('token')
              || localStorage.getItem('requestId')
              || sessionStorage.getItem('token')
              || '';
          } catch (e) {}
          if (!sportToken) return null;
          const hosts = [];
          try { hosts.push(location.origin); } catch (e) {}
          for (const h of [
            'https://api.rccg5fz.com',
            location.origin,
          ]) {
            if (h && hosts.indexOf(h) < 0) hosts.push(h);
          }
          const paths = [
            '/yewu11/v1/user/amount',
            '/yewu13/v1/user/amount',
            '/yewu11/v1/userBalance/getBalance',
            '/yewu13/v1/userBalance/getBalance',
            '/yewu11/v1/user/getUserInfo',
            '/yewu13/v1/user/getUserInfo',
            '/yewu11/v1/user/balance',
            '/yewu13/v1/user/balance',
          ];
          for (const host of hosts) {
            for (const path of paths) {
              try {
                const resp = await fetch(host + path + '?t=' + Date.now(), {
                  method: 'POST',
                  headers: {
                    'content-type': 'application/json',
                    'lang': 'zh',
                    'requestid': sportToken,
                  },
                  body: JSON.stringify({}),
                  credentials: 'include',
                });
                const j = await resp.json();
                const v = pick(j, 0);
                if (v != null) return v;
              } catch (e) {}
            }
          }
          return null;
        }"""
        targets = []
        try:
            targets.append(page)
            for fr in page.frames:
                if fr == page.main_frame:
                    continue
                try:
                    fu = (fr.url or "").lower()
                except Exception:
                    fu = ""
                if any(x in fu for x in ("yewu", "app-h5", "zlshelves", "token=", "ybty", "match")):
                    targets.append(fr)
        except Exception:
            targets = [page]
        for tgt in targets:
            try:
                val = await asyncio.wait_for(tgt.evaluate(ob_js), timeout=6.0)
                if val is None:
                    continue
                v = float(val)
                if 0 <= v < 50_000_000:
                    return v
            except Exception:
                continue

    # 只取明确的体育/场馆钱包字段；忽略 transfer/game（综合站常把中心可转填进这些键）
    js = """async (apiToken) => {
      const SPORT_KEYS = [
        'sportBalance','venueBalance','tyBalance','ybtyBalance',
        'sportWalletBalance','sportsBalance','venueWalletBalance'
      ];
      const CENTER_KEYS = new Set([
        'centerWalletBalance','centerMoney','totalBalance','totalMoney','centerBalance',
        'mainBalance','lobbyBalance','cashBalance','transferBalance','gameBalance',
        'transferableBalance'
      ]);
      const pickSportBal = (obj, depth) => {
        if (!obj || depth > 6) return null;
        if (typeof obj !== 'object') return null;
        for (const k of SPORT_KEYS) {
          if (obj[k] != null && obj[k] !== '' && !isNaN(Number(obj[k]))) {
            const n = Number(obj[k]);
            if (n >= 0 && n < 50000000) return n;
          }
        }
        for (const nest of ['wallets','walletList','accountList','walletInfos']) {
          const items = obj[nest];
          if (!Array.isArray(items)) continue;
          for (const w of items) {
            if (!w || typeof w !== 'object') continue;
            const name = String(w.name || w.walletName || w.type || w.walletType || w.currencyName || '').toLowerCase();
            const looksSport = /sport|venue|体育|场馆|\\bty\\b|ybty|开云/.test(name);
            const looksCenter = /center|中心|main|lobby|大厅|现金|transfer|可转|game/.test(name);
            if (looksCenter && !looksSport) continue;
            if (!looksSport) continue;
            for (const k of SPORT_KEYS.concat(['balance','amount','money','availableBalance','usableBalance'])) {
              if (w[k] != null && !isNaN(Number(w[k]))) {
                const n = Number(w[k]);
                if (n >= 0 && n < 50000000) return n;
              }
            }
          }
        }
        for (const nest of ['data','sportWallet','venueWallet','wallet','finance','result','memberWallet']) {
          if (obj[nest] && typeof obj[nest] === 'object') {
            if (nest === 'centerWallet' || CENTER_KEYS.has(nest)) continue;
            const v = pickSportBal(obj[nest], depth + 1);
            if (v != null) return v;
          }
        }
        return null;
      };
      const token =
        apiToken ||
        localStorage.getItem('token') ||
        localStorage.getItem('X-API-TOKEN') ||
        localStorage.getItem('access_token') ||
        sessionStorage.getItem('token') || '';
      if (!token) return null;
      // 综合站体育入口壳：会员接口余额多半是中心钱包，直接放弃
      const path = (location.pathname || '').toLowerCase();
      if (/\\/game\\/sport(\\/[^\\/]+)?\\/?$/.test(path)) return null;
      const base = location.origin;
      const headers = {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'X-API-CLIENT': 'web',
        'X-API-VERSION': '2.0.0',
        'X-API-SITE': '4002',
        'X-API-TOKEN': token,
        'X-API-UUID': localStorage.getItem('_uuid') || '',
      };
      const paths = [
        '/site/api/v1/user/member/info',
        '/site/api/v1/user/member/jwt',
        '/site/api/v1/user/amount',
        '/site/api/v1/user/balance',
        '/site/api/v1/user/wallet',
      ];
      for (const path of paths) {
        try {
          const resp = await fetch(base + path, {
            method: 'POST',
            headers,
            body: '{}',
            credentials: 'include',
          });
          const j = await resp.json();
          const v = pickSportBal(j, 0);
          if (v != null && v >= 0 && v < 50000000) return v;
        } catch (e) {}
      }
      return null;
    }"""
    try:
        # 入口壳上不要用主文档会员 API（易读到中心钱包）
        try:
            main_url = page.url or ""
        except Exception:
            main_url = ""
        if not _is_portal_sport_shell(main_url):
            val = await asyncio.wait_for(page.evaluate(js, api_token), timeout=8.0)
            if val is not None:
                v = float(val)
                if 0 <= v < 50_000_000:
                    return v
        # 尝试场馆 iframe
        for fr in page.frames:
            if fr == page.main_frame:
                continue
            try:
                fu = fr.url or ""
            except Exception:
                continue
            if _is_portal_sport_shell(fu):
                continue
            try:
                val = await asyncio.wait_for(fr.evaluate(js, api_token), timeout=6.0)
                if val is None:
                    continue
                v = float(val)
                if 0 <= v < 50_000_000:
                    return v
            except Exception:
                continue
    except Exception:
        pass
    return 0.0


async def _scrape_ob_venue_sidebar_balance(page) -> tuple[float | None, str]:
    """
    OB/开云体育场馆左侧栏余额（截图）：
      Hi, {username}
      {50.00}  👁  ↻
    真盘口常在 zlshelves iframe（含 user-pc-new）；须同时命中「Hi,」+ 体育侧栏文案。
    禁止：综合站壳中心钱包、无 Hi 的散落两位小数（曾误读 5.00）、API 中心余额 295。
    amount=0.00 视为有效；未识别返回 None。
    """
    # 揭开 **** 隐藏余额；仅点 eye，不点刷新（避免误导航）
    unmask_js = """() => {
      const body = (document.body && (document.body.innerText || '')) || '';
      if (!/Hi[,，]/i.test(body)) return { clicked: false, reason: 'no_hi' };
      if (/Hi[,，][^\\n]{2,48}\\r?\\n\\s*[0-9]+\\.[0-9]{2}/i.test(body)) {
        return { clicked: false, reason: 'already_visible' };
      }
      const eyes = document.querySelectorAll(
        '[class*="eye" i], [class*="visible" i], [class*="conceal" i]'
      );
      for (const el of eyes) {
        try { el.click(); return { clicked: true, how: 'eye' }; } catch (e) {}
      }
      return { clicked: false, reason: 'no_eye' };
    }"""

    js = """() => {
      const clean = (s) => String(s || '')
        .replace(/,/g, '')
        .replace(/\\u00a0/g, ' ')
        .replace(/[\\u200e\\u200f]/g, '')
        .trim();
      const hiRe = /^Hi[,，\\s]/i;
      const moneyStrict = /^([0-9]+\\.[0-9]{2})$/;
      const body = (document.body && (document.body.innerText || '')) || '';
      // 截图侧栏硬特征：有 Hi 且有体育导航（勿用「开云」——综合站壳也有）
      const sportSidebar = /体育投注|滚球赛事|投注记录/.test(body);
      const hasHi = /Hi[,，]/i.test(body);
      const snippet = body.slice(0, 280).replace(/\\s+/g, ' ');
      if (!hasHi) {
        return { ok: false, balance: null, how: 'no_hi', sportSidebar, hasHi, snippet };
      }
      if (!sportSidebar) {
        return { ok: false, balance: null, how: 'no_sport_sidebar', sportSidebar, hasHi, snippet };
      }

      // 全文：Hi, user\\n50.00
      let mBody = body.match(/Hi[,，]\\s*[^\\n]{2,48}\\r?\\n\\s*([0-9]+\\.[0-9]{2})\\b/i);
      if (!mBody) {
        mBody = body.match(/Hi[,，]\\s*\\S{2,40}[\\s\\u00a0]+([0-9]+\\.[0-9]{2})\\b/i);
      }
      if (mBody) {
        const n = Number(mBody[1]);
        if (!isNaN(n) && n >= 0 && n < 50000000) {
          return {
            ok: true, balance: n, how: 'body_hi_newline', sample: mBody[1],
            sportSidebar, hasHi, snippet,
          };
        }
      }

      const all = Array.from(document.querySelectorAll('div, section, aside, li, span, p, strong, b'));
      let hiEl = null;
      for (const el of all) {
        try {
          const t = clean(el.innerText || el.textContent || '').replace(/\\s+/g, ' ');
          if (!t || t.length > 64) continue;
          if (!hiRe.test(t)) continue;
          if (!/[A-Za-z0-9_\\u4e00-\\u9fff]{2,}/.test(t)) continue;
          if (el.children && el.children.length > 8) continue;
          hiEl = el;
          break;
        } catch (e) {}
      }
      if (!hiEl) return { ok: false, balance: null, how: 'hi_el_miss', sportSidebar, hasHi, snippet };

      const roots = [];
      let cur = hiEl;
      for (let i = 0; i < 6 && cur; i++) { roots.push(cur); cur = cur.parentElement; }
      try {
        let sib = hiEl.nextElementSibling;
        for (let i = 0; i < 6 && sib; i++) { roots.push(sib); sib = sib.nextElementSibling; }
      } catch (e) {}

      const candidates = [];
      const pushMoney = (raw, why, score) => {
        const t = clean(String(raw || '')).replace(/\\s+/g, ' ');
        if (!t || t.length > 24) return;
        const only = t.replace(/[^0-9.]/g, '');
        let m = t.match(moneyStrict) || only.match(/^([0-9]+\\.[0-9]{2})$/);
        if (!m) return;
        const n = Number(m[1]);
        if (isNaN(n) || n < 0 || n >= 50000000) return;
        candidates.push({ n, why, t: m[1], score: score || 0 });
      };

      for (const root of roots) {
        try {
          const raw = String(root.innerText || root.textContent || '');
          const lines = raw.split(/\\r?\\n/).map((x) => clean(x).replace(/\\s+/g, ' ')).filter(Boolean);
          for (let i = 0; i < lines.length; i++) {
            if (hiRe.test(lines[i]) && lines[i + 1]) {
              pushMoney(lines[i + 1], 'after_hi_line', 100);
            }
          }
          const blob = clean(raw).replace(/\\s+/g, ' ');
          const m2 = blob.match(/Hi[,，]\\s*\\S{2,40}\\s+([0-9]+\\.[0-9]{2})\\b/i);
          if (m2) pushMoney(m2[1], 'hi_same_block', 90);
        } catch (e) {}
      }

      // 眼睛左侧金额（截图布局）
      try {
        document.querySelectorAll('[class*="eye" i], [class*="visible" i]').forEach((el) => {
          const prev = el.previousElementSibling;
          if (prev) pushMoney(prev.innerText || prev.textContent || '', 'before_eye', 85);
          let p = el.parentElement;
          for (let i = 0; i < 2 && p; i++) {
            const txt = clean(p.innerText || '').replace(/\\s+/g, ' ');
            // 父块应紧邻 Hi，避免整页
            if (txt.length < 80) {
              const m = txt.match(/\\b([0-9]+\\.[0-9]{2})\\b/);
              if (m) pushMoney(m[1], 'near_eye', 70);
            }
            p = p.parentElement;
          }
        });
      } catch (e) {}

      if (!candidates.length) {
        return { ok: false, balance: null, how: 'hi_no_money', sportSidebar, hasHi, snippet };
      }
      // 只认 Hi/眼睛邻近；同档取较大额（50 盖过误采的 5）
      candidates.sort((a, b) => (b.score - a.score) || (b.n - a.n));
      const prefer = candidates[0];
      return {
        ok: true, balance: prefer.n, how: prefer.why, sample: prefer.t,
        sportSidebar, hasHi, snippet, nCand: candidates.length,
      };
    }"""

    def _frame_score(fr) -> int:
        try:
            fu = (fr.url or "").lower()
        except Exception:
            fu = ""
        score = 0
        # 开云 H5 / zlshelves 是真侧栏所在（含 user-pc-new SPA）
        if any(x in fu for x in ("zlshelves", "kaiyun", "ybty", "yewu", "app-h5", "token=")):
            score += 10
        if "user-pc" in fu:
            score += 2  # 仍要扫，但靠 sportSidebar 过滤个人中心
        if fr == getattr(page, "main_frame", None):
            score += 1  # 综合站壳通常无 Hi 侧栏
        return score

    targets: list = []
    try:
        scored = []
        for fr in list(page.frames or []):
            try:
                fu = str(fr.url or "")[:160]
            except Exception:
                fu = "?"
            scored.append((_frame_score(fr), fr, fu))
        scored.sort(key=lambda x: -x[0])
        targets = [fr for _, fr, _ in scored]
        logger.info(
            "OB sidebar frames (%d): %s",
            len(scored),
            " | ".join(f"{sc}:{fu}" for sc, _, fu in scored[:10]),
        )
    except Exception:
        targets = [page]

    async def _scan_once() -> tuple[float | None, str]:
        # 刷新 targets：H5 iframe 可能晚于主壳出现
        nonlocal targets
        try:
            scored = []
            for fr in list(page.frames or []):
                try:
                    fu = str(fr.url or "")[:160]
                except Exception:
                    fu = "?"
                scored.append((_frame_score(fr), fr, fu))
            scored.sort(key=lambda x: -x[0])
            targets = [fr for _, fr, _ in scored]
        except Exception:
            pass

        for tgt in targets[:5]:
            try:
                await asyncio.wait_for(tgt.evaluate(unmask_js), timeout=1.5)
            except Exception:
                pass
        try:
            await page.wait_for_timeout(200)
        except Exception:
            pass

        best: tuple[int, float, str, str] | None = None  # (hi_quality, bal, note, url)
        saw_empty_h5 = False
        for tgt in targets:
            try:
                fu = str(getattr(tgt, "url", "") or "")[:160]
            except Exception:
                fu = ""
            try:
                data = await asyncio.wait_for(tgt.evaluate(js), timeout=3.0)
            except Exception as e:
                logger.info("OB sidebar eval fail url=%s err=%s", fu, type(e).__name__)
                continue
            if not isinstance(data, dict):
                continue
            if not data.get("ok"):
                how_miss = str(data.get("how") or "")
                if "zlshelves" in fu.lower() and how_miss in ("no_hi", "hi_no_money", "hi_el_miss"):
                    saw_empty_h5 = True
                logger.info(
                    "OB sidebar miss how=%s url=%s hasHi=%s sportSidebar=%s snippet=%s",
                    data.get("how"),
                    fu,
                    data.get("hasHi"),
                    data.get("sportSidebar"),
                    (data.get("snippet") or "")[:140],
                )
                continue
            try:
                bal = float(data.get("balance"))
            except (TypeError, ValueError):
                continue
            if bal < 0 or bal >= 50_000_000:
                continue
            if not data.get("sportSidebar"):
                continue
            how = str(data.get("how") or "dom")
            hi_quality = 1 if how in (
                "body_hi_newline", "after_hi_line", "hi_same_block", "before_eye", "near_eye",
            ) else 0
            if not hi_quality:
                continue
            note = f"ob_sidebar:{how}"
            logger.info(
                "OB sidebar balance=%.2f via %s url=%s sample=%s",
                bal, note, fu, data.get("sample"),
            )
            rank = (hi_quality, bal)
            if best is None or rank > (best[0], best[1]):
                best = (hi_quality, bal, note, fu)
                if how in ("body_hi_newline", "after_hi_line", "hi_same_block"):
                    return bal, note
        if best:
            logger.info("OB sidebar pick balance=%.2f via %s url=%s", best[1], best[2], best[3])
            return best[1], best[2]
        if saw_empty_h5:
            return None, "ob_sidebar:h5_loading"
        return None, "ob_sidebar:miss"

    # H5 iframe 偶发空 body，短重试（曾误报 0 / 中心钱包）
    bal, src = await _scan_once()
    if bal is not None:
        return bal, src
    if src == "ob_sidebar:h5_loading":
        for _ in range(3):
            try:
                await page.wait_for_timeout(800)
            except Exception:
                pass
            bal, src = await _scan_once()
            if bal is not None:
                return bal, src
            if src != "ob_sidebar:h5_loading":
                break
    return None, src or "ob_sidebar:miss"


async def _scrape_balance_from_page(
    page, *, site_code: str = "", session_token: str = ""
) -> tuple[float, bool]:
    """
    仅刮取体育场馆内余额（OB 侧栏 Hi 下方 / 平博顶栏 CNY）。
    返回 (amount, recognized)：recognized=True 表示已定位到场馆钱包 UI（金额可为 0.00）。
    """
    import re
    import statistics

    from app.services.bookmakers.venue_entry import (
        dismiss_blocking_modals,
        is_in_sportsbook,
        page_has_trade_password_modal,
    )

    code = (site_code or "").lower()
    try:
        from app.services.bookmakers.venue_entry import page_has_system_error
        if await page_has_trade_password_modal(page) or await page_has_system_error(page):
            logger.info("balance scrape skip %s: trade-password/system-error — do not touch page", code)
            return 0.0, False
    except Exception:
        pass
    try:
        await dismiss_blocking_modals(page)
    except Exception:
        pass

    try:
        in_book = await is_in_sportsbook(page)
    except Exception:
        in_book = False
    if not in_book:
        # 场馆外（大厅/中心钱包）一律不采，避免把中心余额当可投注余额
        return 0.0, False

    try:
        main_url = page.url or ""
    except Exception:
        main_url = ""
    portal_shell = _is_portal_sport_shell(main_url)

    # OB：优先截图侧栏「Hi, 用户名」下方金额（场馆可投余额）；含 0.00
    if code == "ob":
        sidebar_bal, sidebar_src = await _scrape_ob_venue_sidebar_balance(page)
        if sidebar_bal is not None:
            logger.info("balance scrape ob use sidebar %.2f (%s)", sidebar_bal, sidebar_src)
            return float(sidebar_bal), True
        api_bal = await _fetch_balance_via_page_api(
            page, site_code=code, session_token=session_token
        )
        # API 仅作侧栏未识别时的回退；综合站壳上的会员接口常返回中心钱包（如 295），须拒绝
        if api_bal > 0:
            try:
                mu = (page.url or "").lower()
            except Exception:
                mu = ""
            portal_shell_url = _is_portal_sport_shell(mu) or bool(
                re.search(r"/game/sport/[^/?#]+/?$", (mu or "").split("?")[0] or "")
            )
            if portal_shell_url and not any(
                x in mu for x in ("token=", "app-h5", "yewu", "zlshelves")
            ):
                logger.info(
                    "balance scrape ob ignore API %.2f on portal shell (need Hi sidebar)",
                    api_bal,
                )
            else:
                logger.info("balance scrape ob use API %.2f (sidebar miss)", api_bal)
                return float(api_bal), True

    # 综合站体育入口壳：禁止刮主文档顶栏（几乎一定是中心钱包）
    if portal_shell and code == "ob":
        # 仅当存在真盘口 iframe 时才允许从 iframe DOM 采；否则返回 0
        has_venue_frame = False
        try:
            for fr in page.frames:
                if fr == page.main_frame:
                    continue
                try:
                    fu = fr.url or ""
                except Exception:
                    continue
                if fu and not _is_portal_sport_shell(fu) and (
                    "token=" in fu.lower()
                    or "yewu" in fu.lower()
                    or "app-h5" in fu.lower()
                    or "zlshelves" in fu.lower()
                ):
                    has_venue_frame = True
                    break
        except Exception:
            has_venue_frame = False
        if not has_venue_frame:
            logger.info(
                "balance scrape skip ob portal shell (no H5 frame): %s",
                main_url[:120],
            )
            return 0.0, False

    js = """(siteCode) => {
      const clean = (s) => String(s || '').replace(/,/g, '').replace(/\\u00a0/g, ' ').replace(/[\\u200e\\u200f]/g, '');
      const texts = [];
      const labeled = [];
      const push = (t, isLabeled) => {
        t = clean(t).trim();
        if (!t || t.length > 120) return;
        texts.push(t);
        if (isLabeled) labeled.push(t);
      };
      // 优先场馆/体育余额区域
      const preferSel = [
        '[class*="sport" i][class*="balance" i]',
        '[class*="venue" i][class*="balance" i]',
        '[class*="ty" i][class*="balance" i]',
        '[class*="wallet" i]',
        '[class*="balance" i]',
        '[class*="header" i]',
        'header',
        'nav',
        '[class*="toolbar" i]',
        '[class*="top-bar" i]',
        '[class*="topBar" i]'
      ].join(',');
      try {
        document.querySelectorAll(preferSel).forEach((el) => {
          try {
            const t = el.innerText || el.textContent || '';
            const lab = /体育|场馆|可投|可转|sport|venue|ty\\b|余额|Balance|CNY|¥|￥|存款|钱包/i.test(t);
            push(t, lab);
          } catch (e) {}
        });
      } catch (e) {}
      // 平博顶栏：单独抓紧挨「CNY / 存款」的短文本
      try {
        document.querySelectorAll('span, div, strong, b').forEach((el) => {
          try {
            const t = clean(el.innerText || el.textContent || '').trim();
            if (!t || t.length > 24) return;
            if (/^[0-9]+(?:\\.[0-9]{1,2})?\\s*CNY$/i.test(t) || /^CNY\\s*[0-9]+(?:\\.[0-9]{1,2})?$/i.test(t)) {
              push(t, true);
            }
          } catch (e) {}
        });
      } catch (e) {}
      document.querySelectorAll('span, div, strong, b, p, label').forEach((el) => {
        if (el.children && el.children.length > 2) return;
        try {
          const t = clean(el.innerText || el.textContent || '').trim();
          if (!t || t.length > 40) return;
          if (!/\\d/.test(t)) return;
          const lab = /体育|场馆|可投|sport|venue|余额|Balance|CNY|¥/i.test(t);
          push(t, lab);
        } catch (e) {}
      });
      return { siteCode, texts: texts.slice(0, 300), labeled: labeled.slice(0, 120) };
    }"""

    payloads: list[dict] = []
    # 入口壳主文档顶栏常是中心钱包：跳过主文档，只采真盘口 frame
    if not (portal_shell and code == "ob"):
        try:
            payloads.append(await page.evaluate(js, code))
        except Exception:
            pass
    try:
        for fr in page.frames:
            if fr == page.main_frame and portal_shell and code == "ob":
                continue
            try:
                fu = (fr.url or "").lower()
            except Exception:
                fu = ""
            if portal_shell and code == "ob":
                if not fu or _is_portal_sport_shell(fu):
                    continue
                if not any(x in fu for x in ("token=", "yewu", "app-h5", "zlshelves", "ybty", "match")):
                    continue
            try:
                payloads.append(await fr.evaluate(js, code))
            except Exception:
                continue
    except Exception:
        pass

    venue_hits: list[float] = []
    currency_hits: list[float] = []
    cny_header_hits: list[float] = []  # 「16.78 CNY」顶栏钱包，优先采用
    candidates: list[float] = []
    venue_patterns = [
        re.compile(r"(?:体育|场馆|可投注|可转账|ty)\s*(?:余额|钱包)?\s*[:：]?\s*([0-9]+(?:\.[0-9]+)?)", re.I),
        re.compile(r"(?:Sport|Venue)\s*(?:Balance|Wallet)?\s*[:：]?\s*([0-9]+(?:\.[0-9]+)?)", re.I),
    ]
    # 平博顶栏：钱包图标旁「16.78 CNY」
    cny_header_patterns = [
        re.compile(r"\b([0-9]+(?:\.[0-9]{1,2}))\s*CNY\b", re.I),
        re.compile(r"\bCNY\s*([0-9]+(?:\.[0-9]{1,2}))\b", re.I),
        re.compile(r"[¥￥]\s*([0-9]+(?:\.[0-9]{1,2}))\b"),
    ]
    patterns = [
        re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*CNY\b", re.I),
        re.compile(r"\bCNY\s*([0-9]+(?:\.[0-9]+)?)", re.I),
        re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*USD\b", re.I),
        re.compile(r"[¥￥]\s*([0-9]+(?:\.[0-9]+)?)"),
        re.compile(r"余\s*额\s*[:：]?\s*([0-9]+(?:\.[0-9]+)?)"),
        re.compile(r"可用\s*(?:余额|额度)?\s*[:：]?\s*([0-9]+(?:\.[0-9]+)?)"),
        re.compile(r"Balance\s*[:：]?\s*([0-9]+(?:\.[0-9]+)?)", re.I),
    ]
    # 明确排除中心钱包文案旁的金额
    center_line = re.compile(r"中心\s*钱包|center\s*wallet|大厅\s*余额|主账户", re.I)
    bare_money = re.compile(r"^(?:[¥￥]\s*)?([0-9]+\.[0-9]{2})$")

    def _add(pool: list, v: float) -> None:
        if 0 < v < 50_000_000 and not (1900 <= v <= 2100 and float(v).is_integer()):
            pool.append(v)

    def _is_wallet_money(v: float) -> bool:
        """两位小数金额更像钱包余额；整数 1~20 更像节次/比分/菜单。"""
        try:
            fv = float(v)
        except (TypeError, ValueError):
            return False
        if fv <= 0:
            return False
        # 恰好两位小数（16.78 / 1.00）
        cents = round(fv * 100)
        if abs(fv * 100 - cents) < 1e-6:
            return True
        return fv >= 50  # 大额整数也可能是余额

    for payload in payloads:
        for raw in (payload.get("labeled") or []) + (payload.get("texts") or []):
            text = (raw or "").replace(",", "").replace("\u00a0", " ")
            if center_line.search(text) and not re.search(r"体育|场馆|sport|venue", text, re.I):
                continue
            for pat in cny_header_patterns:
                for m in pat.finditer(text):
                    try:
                        ctx = text[max(0, m.start() - 28) : m.end() + 20]
                        # 投注单内金额不是钱包
                        if re.search(r"总注金|潜在奖金|最低投注|投注金额|风险|本金|可赢", ctx):
                            continue
                        v = float(m.group(1))
                        _add(cny_header_hits, v)
                        if re.search(r"存款|钱包", ctx):
                            _add(cny_header_hits, v)  # 顶栏「xx CNY 存款」加权
                    except Exception:
                        continue
            for pat in venue_patterns:
                for m in pat.finditer(text):
                    try:
                        _add(venue_hits, float(m.group(1)))
                    except Exception:
                        continue
            for pat in patterns:
                for m in pat.finditer(text):
                    try:
                        _add(currency_hits, float(m.group(1)))
                    except Exception:
                        continue
            for line in text.splitlines():
                line = line.strip()
                if center_line.search(line):
                    continue
                m = bare_money.match(line)
                if not m:
                    continue
                try:
                    _add(candidates, float(m.group(1)))
                except Exception:
                    continue

    def _looks_like_odds(vals: list[float]) -> bool:
        """一堆 1.01~5 的小数往往是盘口赔率，不是钱包。"""
        if len(vals) < 3:
            return False
        # 收窄到典型亚赔区间，避免把 16.78 这种钱包余额当成赔率
        oddsish = [v for v in vals if 1.01 <= v <= 8.0 and not _is_wallet_money(v)]
        return len(oddsish) >= max(3, int(len(vals) * 0.55))

    def _pick_balance(pool: list[float]) -> float:
        if not pool:
            return 0.0
        cleaned = list(pool)
        if _looks_like_odds(cleaned):
            # 保留「像钱」的两位小数，丢掉典型亚赔
            moneyish = [v for v in cleaned if _is_wallet_money(v) and (v < 1.01 or v > 8.0 or v >= 10)]
            if not moneyish:
                moneyish = [v for v in cleaned if _is_wallet_money(v)]
            odds_out = [v for v in cleaned if v < 1.0 or v > 30.0]
            cleaned = moneyish or odds_out
        if not cleaned:
            return 0.0
        # 去掉极端离群后取较大的稳定值（场馆顶栏余额通常比一堆赔率大）
        cleaned = sorted(cleaned)
        if len(cleaned) >= 4:
            try:
                med = float(statistics.median(cleaned))
                band = [v for v in cleaned if abs(v - med) / max(med, 1.0) <= 0.35]
                # 若带 CNY 的大额被 median 挤掉，仍保留 >=10 的钱包候选
                big = [v for v in cleaned if v >= 10 and _is_wallet_money(v)]
                if band:
                    cleaned = sorted(set(band + big)) if big else band
            except Exception:
                pass
        return float(max(cleaned))

    # 1) 顶栏「xx.xx CNY」最可信（平博截图即为这种）
    if cny_header_hits:
        labeled_money = [v for v in cny_header_hits if _is_wallet_money(v)]
        pool = labeled_money or cny_header_hits
        # 多候选时取众数/较小稳定值，避免投注单大额「总注金」盖过真余额
        try:
            from collections import Counter

            cnt = Counter(round(float(v), 2) for v in pool)
            # 出现次数最多的优先；并列取较小（顶栏真余额通常小于误刮的总注金）
            best = sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
            picked = float(best)
        except Exception:
            picked = _pick_balance(pool)
        if picked > 0:
            logger.info("balance scrape %s via CNY header: %s from %s", code, picked, sorted(set(cny_header_hits))[:12])
            return float(picked), True
    # 场馆内：优先带「体育/场馆」标签的金额；其次币种金额；禁止裸赔率冒充余额
    if venue_hits:
        picked = _pick_balance(venue_hits)
        if picked > 0:
            return float(picked), True
    if currency_hits:
        picked = _pick_balance(currency_hits)
        if picked > 0:
            return float(picked), True
    # bare 候选极易混入亚赔；仅在非赔率簇时采用，且要求像钱包金额
    bare = [v for v in candidates if _is_wallet_money(v)]
    if bare and not _looks_like_odds(candidates):
        picked = _pick_balance(bare)
        if picked > 0:
            return float(picked), True
    return 0.0, False


@app.post("/balance")
async def fetch_balance(req: BalanceRequest):
    """读取体育场馆内余额；不在场馆则返回缓存/0，绝不刮中心钱包。"""
    from app.services.bookmakers.plugins.ob.odds import sanitize_token
    from app.services.bookmakers.session_blob import apply_session_blob, is_session_blob
    from app.services.bookmakers.venue_entry import (
        dismiss_blocking_modals,
        is_in_sportsbook,
        page_has_trade_password_modal,
    )

    site_code = (req.site_code or "ob").lower()
    token = sanitize_token(req.session_token)
    lane = await _get_lane(req.base_url, site_code)
    # 盘口同步占用页面时：先短等锁释放再刮（OB 侧栏 0.00 也必须能覆盖错误的中心钱包缓存）
    if lane.lock.locked() and (lane.busy_op or "").startswith("odds"):
        waited = 0.0
        while lane.lock.locked() and (lane.busy_op or "").startswith("odds") and waited < 18.0:
            await asyncio.sleep(0.4)
            waited += 0.4
        if lane.lock.locked() and (lane.busy_op or "").startswith("odds"):
            sess_busy = site_sessions.get(req.base_url)
            cached = float(getattr(sess_busy, "last_balance", 0) or 0) if sess_busy else 0.0
            # 侧栏已识别过的空钱包（含 0.00）也算有效缓存
            recognized_empty = bool(getattr(sess_busy, "balance_recognized", False)) and cached <= 0
            return {
                "ok": cached > 0 or recognized_empty,
                "balance": cached,
                "message": "盘口同步中，返回场馆余额缓存",
                "lane": lane.key,
                "cached": True,
                "balance_source": "venue_cache" if (cached > 0 or recognized_empty) else "busy",
                "in_sportsbook": True,
            }
    sess = site_sessions.get(req.base_url)
    if not sess or not sess.page or sess.page.is_closed():
        sess = await site_sessions.ensure_for_odds(
            base_url=req.base_url,
            session_token=token,
            site_code=site_code,
            recreate=False,
        )
    if not sess or not sess.page or sess.page.is_closed():
        return {
            "ok": False,
            "balance": 0,
            "message": "无有效长连接，请先验证并进入场馆",
            "lane": lane.key,
            "balance_source": "none",
        }
    if site_code:
        sess.site_code = site_code

    try:
        await dismiss_blocking_modals(sess.page)
    except Exception:
        pass
    try:
        if await page_has_trade_password_modal(sess.page):
            await dismiss_blocking_modals(sess.page)
            cached = float(sess.last_balance or 0)
            if await page_has_trade_password_modal(sess.page):
                return {
                    "ok": cached > 0,
                    "balance": cached,
                    "message": "检测到交易密码弹窗（已尝试关闭）。请在浏览器中取消/关闭后重试；未提交空密码",
                    "lane": lane.key,
                    "site_code": sess.site_code or site_code,
                    "url": (getattr(sess.page, "url", "") or "")[:160],
                    "cached": cached > 0,
                    "balance_source": "trade_password_blocked" if cached <= 0 else "venue_cache",
                    "in_sportsbook": True,
                }
    except Exception:
        pass

    try:
        in_book = await is_in_sportsbook(sess.page)
    except Exception:
        in_book = False
    if not in_book:
        cached = float(sess.last_balance or 0)
        return {
            "ok": cached > 0,
            "balance": cached,
            "message": "当前不在体育场馆，已返回场馆余额缓存（未读中心钱包）" if cached > 0 else "请先进入体育场馆后再同步余额",
            "lane": lane.key,
            "site_code": sess.site_code or site_code,
            "url": (getattr(sess.page, "url", "") or "")[:160],
            "cached": cached > 0,
            "balance_source": "venue_cache" if cached > 0 else "not_in_venue",
            "in_sportsbook": False,
        }

    api_hint = token
    if token and sess.page and not sess.page.is_closed():
        try:
            if is_session_blob(token):
                api_hint = await apply_session_blob(sess.context, sess.page, token)
            else:
                await apply_session_blob(sess.context, sess.page, token)
                api_hint = token
        except Exception:
            api_hint = token
    bal = 0.0
    recognized = False
    try:
        bal, recognized = await asyncio.wait_for(
            _scrape_balance_from_page(
                sess.page, site_code=site_code, session_token=api_hint or token
            ),
            timeout=12.0,
        )
        if recognized:
            # 含 0.00：侧栏明确读到场馆钱包空，覆盖错误的中心钱包缓存（如 295）
            sess.last_balance = float(bal)
            sess.balance_recognized = True
            return {
                "ok": True,
                "balance": float(bal),
                "message": "ok" if bal > 0 else "ok (venue wallet empty)",
                "lane": lane.key,
                "site_code": sess.site_code or site_code,
                "url": (getattr(sess.page, "url", "") or "")[:160],
                "cached": False,
                "balance_source": "venue" if bal > 0 else "venue_empty",
                "in_sportsbook": True,
            }
        if bal > 0:
            sess.last_balance = float(bal)
            sess.balance_recognized = True
        elif float(sess.last_balance or 0) > 0:
            cached = float(sess.last_balance)
            # 历史误把亚赔当余额（如 0.56/1.85）-> 丢弃脏缓存
            if 0 < cached <= 30.0 and site_code in ("ob", "pinnacle"):
                # 真实场馆余额也可能很小；仅当「本次刮取明确失败且缓存像赔率簇」才清
                # 这里：小于 1 的两位小数更像赔率碎片
                if cached < 1.0:
                    sess.last_balance = 0.0
                    cached = 0.0
            if cached > 0:
                bal = cached
                return {
                    "ok": True,
                    "balance": bal,
                    "message": "ok (venue cached)",
                    "lane": lane.key,
                    "site_code": sess.site_code or site_code,
                    "url": (getattr(sess.page, "url", "") or "")[:160],
                    "cached": True,
                    "balance_source": "venue_cache",
                    "in_sportsbook": True,
                }
    except Exception as e:
        cached = float(sess.last_balance or 0)
        if cached > 0:
            return {
                "ok": True,
                "balance": cached,
                "message": f"场馆余额刮取失败，返回缓存: {e}",
                "lane": lane.key,
                "cached": True,
                "balance_source": "venue_cache",
                "in_sportsbook": True,
            }
        return {
            "ok": False,
            "balance": 0,
            "message": f"读取场馆余额失败: {e}",
            "lane": lane.key,
            "balance_source": "error",
        }
    return {
        "ok": bal > 0,
        "balance": float(bal),
        "message": "ok" if bal > 0 else "场馆页未识别到体育钱包余额",
        "lane": lane.key,
        "site_code": sess.site_code or site_code,
        "url": (getattr(sess.page, "url", "") or "")[:160],
        "balance_source": "venue" if bal > 0 else "venue_empty",
        "in_sportsbook": True,
    }


@app.post("/session/close")
async def session_close(req: SessionCloseRequest):
    """断开本机长连接浏览器会话（关闭隐藏 Chromium）。"""
    base = (req.base_url or "").strip()
    if not base:
        return {"ok": False, "message": "缺少 base_url"}
    site_code = (req.site_code or "ob").lower()
    lane = await _get_lane(base, site_code)
    # 尽量占锁，避免同步中途被关掉；占不到也强制 invalidate
    acquired = False
    try:
        await asyncio.wait_for(lane.lock.acquire(), timeout=2.0)
        acquired = True
    except asyncio.TimeoutError:
        pass
    try:
        if acquired:
            lane.busy_op = "disconnect"
        had = bool(site_sessions.get(base))
        await site_sessions.invalidate(base)
        return {
            "ok": True,
            "message": "已断开浏览器会话" if had else "无活动浏览器会话",
            "closed": had,
            "lane": lane.key,
        }
    finally:
        if acquired:
            lane.busy_op = None
            lane.clear_login_priority()
            try:
                lane.lock.release()
            except Exception:
                pass


@app.post("/odds/sync")
async def odds_sync(req: OddsSyncRequest):
    return await _run_odds_sync(req)


@app.post("/odds/sync-live")
async def odds_sync_live(req: OddsSyncRequest):
    req.live_only = True
    # 滚球全量：足球+篮球，抬高上限避免截断
    if req.limit > 800:
        req.limit = 800
    return await _run_odds_sync(req)


@app.post("/bet/place")
async def bet_place(req: BetPlaceRequest):
    from decimal import Decimal

    from app.services.bookmakers import site_bet as _site_bet_mod

    place_site_bet = _site_bet_mod.place_site_bet

    token = sanitize_token(req.session_token)
    if not token:
        return {"ok": False, "message": "缺少 session token"}
    site_code = (req.site_code or "ob").lower()
    lane = await _get_lane(req.base_url, site_code)

    # 设置下单优先级（让 odds-sync 让路）
    lane.set_bet_priority(40.0)

    try:
        await asyncio.wait_for(lane.lock.acquire(), timeout=30.0)
    except asyncio.TimeoutError:
        lane.clear_bet_priority()
        return {
            "ok": False,
            "busy": True,
            "message": f"本站浏览器正忙（{lane.busy_op or '占用中'}），请稍后再下单",
            "lane": lane.key,
        }

    lane.busy_op = "bet"
    try:
        sess = site_sessions.find(base_url=req.base_url, site_code=site_code)
        page = sess.page if sess and sess.page and not sess.page.is_closed() else None
        if not page:
            return {
                "ok": False,
                "message": "无有效长连接浏览器，请先验证登录并进入场馆（禁止另开窗口下单）",
                "lane": lane.key,
            }
        plug = get_plugin(site_code)
        result = await plug.place_bet(
            page,
            base_url=req.base_url,
            session_token=token,
            match_external_id=req.match_external_id,
            selection=req.selection,
            odds=float(req.odds),
            stake=Decimal(str(req.stake)),
            bet_type=req.bet_type,
            odds_data=req.odds_data or {},
        )
        if result is None:
            result = await place_site_bet(
                site_code=site_code,
                base_url=req.base_url,
                session_token=token,
                match_external_id=req.match_external_id,
                selection=req.selection,
                odds=float(req.odds),
                stake=Decimal(str(req.stake)),
                bet_type=req.bet_type,
                odds_data=req.odds_data or {},
                page=page,
                headed=False,
            )
    except Exception as e:
        return {"ok": False, "message": f"下单异常: {e}", "lane": lane.key}
    finally:
        lane.busy_op = None
        lane.clear_bet_priority()
        lane.lock.release()
    return {
        "ok": result.ok,
        "message": result.message,
        "external_bet_id": result.external_bet_id,
        "balance_after": float(result.balance_after or 0),
        "site_code": site_code,
        "lane": lane.key,
    }


@app.post("/bets/history")
async def bets_history(req: BetHistoryRequest):
    """拉取站点近期注单（OB API；平博暂返回空，依赖本地结算）。"""
    from app.services.bookmakers.plugins.ob import orders as _ob_orders

    fetch_ob_orders = _ob_orders.fetch_ob_orders
    history_timeout_sec = max(20.0, float(os.getenv("GATE_BET_HISTORY_TIMEOUT_SEC") or 60.0))

    site_code = (req.site_code or "ob").lower()
    lane = await _get_lane(req.base_url, site_code)
    try:
        await asyncio.wait_for(lane.lock.acquire(), timeout=10.0)
    except asyncio.TimeoutError:
        return {
            "ok": False,
            "busy": True,
            "message": f"本站浏览器正忙（{lane.busy_op or '占用中'}）",
            "orders": [],
        }
    lane.busy_op = "bet-history"
    try:
        sess = site_sessions.find(base_url=req.base_url, site_code=site_code)
        page = sess.page if sess and sess.page and not sess.page.is_closed() else None
        if not page:
            return {
                "ok": False,
                "message": "无有效长连接，请先验证并进入场馆",
                "orders": [],
            }
        if site_code == "ob":
            try:
                return await asyncio.wait_for(
                    fetch_ob_orders(
                        page=page,
                        session_token=sanitize_token(req.session_token),
                        days=max(1, min(int(req.days or 3), 14)),
                    ),
                    timeout=history_timeout_sec,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "bets/history timeout site=%s lane=%s timeout=%.1fs",
                    site_code,
                    lane.key,
                    history_timeout_sec,
                )
                return {
                    "ok": False,
                    "message": f"注单历史查询超时（>{history_timeout_sec:.0f}s）",
                    "orders": [],
                    "timeout": True,
                }
        # 平博：暂无稳定公开列表 API，返回空由本地完场结算覆盖
        return {
            "ok": True,
            "orders": [],
            "message": "平博注单以本地下单记录 + 完场结算为准",
            "site_code": site_code,
        }
    except Exception as e:
        return {"ok": False, "message": str(e), "orders": []}
    finally:
        lane.busy_op = None
        lane.lock.release()


if __name__ == "__main__":
    loop = "asyncio"
    http = "auto"
    try:
        import uvloop  # noqa: F401

        loop = "uvloop"
    except Exception:
        pass
    try:
        import httptools  # noqa: F401

        http = "httptools"
    except Exception:
        pass
    # 默认仅本机；线上勿对公网暴露。需要 Docker 访问时可设 GATE_HOST=0.0.0.0
    host = (os.getenv("GATE_HOST") or "127.0.0.1").strip() or "127.0.0.1"
    port = int(os.getenv("GATE_PORT") or "9277")
    print(f"Browser Gate 已启动: http://{host}:{port}")
    print(
        f"模式: 每站点独立车道 + {loop}/{http}；auth={'on' if _GATE_AUTH_REQUIRED else 'off'}"
    )
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info",
        loop=loop,
        http=http,
        limit_concurrency=100,
        backlog=512,
        timeout_keep_alive=30,
        access_log=False,
    )
