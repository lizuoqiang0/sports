"""
开云体育（YBTY）注单列表拉取 — 供投注记录页同步输赢。
"""
from __future__ import annotations

import asyncio
import base64
import gzip
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from app.services.bookmakers.plugins.ob.odds import sanitize_token

logger = logging.getLogger(__name__)


def _decode_gzip_data(data):
    """解码 OB API 返回的 gzip+base64 压缩 data 字段。"""
    if isinstance(data, str) and data:
        # gzip base64
        if data.startswith("H4sIAA"):
            try:
                raw = base64.b64decode(data)
                decompressed = gzip.decompress(raw)
                return json.loads(decompressed)
            except Exception:
                pass
        # 普通 JSON 字符串
        try:
            return json.loads(data)
        except Exception:
            pass
    return data

_ORDER_PATHS = (
    "/yewurecord/order/betRecord/getOrderListPB",
)


def _map_status(raw: Any) -> str:
    s = str(raw or "").strip().lower()
    code = str(raw)
    # 常见：0未结算 1全赢 2全输 3走水/取消 4半赢 5半输
    if code in ("4", "half_win", "半赢", "赢半"):
        return "half_win"
    if code in ("5", "half_loss", "半输", "输半"):
        return "half_loss"
    if code in ("1", "won", "win", "已赢", "赢"):
        return "won"
    if code in ("2", "lost", "lose", "已输", "输"):
        return "lost"
    if code in ("3", "void", "refund", "draw", "push", "走水", "取消", "退款"):
        return "void"
    if "半赢" in s or "赢半" in s:
        return "half_win"
    if "半输" in s or "输半" in s:
        return "half_loss"
    if "赢" in s and "输" not in s:
        return "won"
    if "输" in s:
        return "lost"
    if any(x in s for x in ("走", "取消", "退", "void", "refund")):
        return "void"
    if any(x in s for x in ("确认", "接受", "进行", "未结算", "running", "open", "accept")):
        return "accepted"
    return "accepted"


def _normalize_row(row: dict) -> Optional[dict]:
    if not isinstance(row, dict):
        return None
    # 嵌套
    inner = row.get("data") if isinstance(row.get("data"), dict) else row
    order_no = (
        inner.get("orderNo")
        or inner.get("orderId")
        or inner.get("betOrderNo")
        or inner.get("id")
        or row.get("orderNo")
    )
    if not order_no:
        return None
    stake = (
        inner.get("betAmount")
        or inner.get("betMoney")
        or inner.get("stake")
        or inner.get("seriesSum")
        or 0
    )
    try:
        stake_f = float(stake)
        # 开云内部常有 *100
        if stake_f >= 100 and stake_f % 1 == 0 and stake_f / 100 <= 50000:
            # 1000 → 10.00 启发式：若像分
            if stake_f >= 100 and float(inner.get("oddFinally") or inner.get("oddsValues") or 0) < 50:
                stake_f = stake_f / 100.0
    except Exception:
        stake_f = 0.0

    odds_raw = (
        inner.get("oddFinally")
        or inner.get("oddsValues")
        or inner.get("odds")
        or 0
    )
    try:
        odds_f = float(odds_raw)
        if odds_f > 100:  # ov
            odds_f = odds_f / 100000.0
    except Exception:
        odds_f = 0.0

    payout = (
        inner.get("settleAmount")
        or inner.get("profitAmount")
        or inner.get("winAmount")
        or inner.get("maxWinMoney")
        or None
    )
    try:
        payout_f = float(payout) if payout is not None else None
        if payout_f is not None and payout_f >= 100 and stake_f and payout_f > stake_f * 20:
            payout_f = payout_f / 100.0
    except Exception:
        payout_f = None

    status = _map_status(
        inner.get("betStatus")
        or inner.get("orderStatus")
        or inner.get("status")
        or inner.get("outcome")
        or row.get("betStatus")
    )
    if status == "won" and payout_f is None and stake_f and odds_f:
        payout_f = round(stake_f * odds_f, 2)
    if status == "half_win" and payout_f is None and stake_f and odds_f:
        # 半赢：本金 + 半利
        payout_f = round(stake_f + stake_f * (odds_f - 1) / 2, 2)
    if status == "half_loss" and payout_f is None and stake_f:
        # 半输：退半本
        payout_f = round(stake_f / 2, 2)
    if status == "lost" and payout_f is None:
        payout_f = 0.0
    if status == "void" and payout_f is None:
        payout_f = stake_f

    play_opt = str(
        inner.get("playOptions")
        or inner.get("oddsType")
        or inner.get("playOptionName")
        or ""
    ).lower()
    selection = "under"
    if "under" in play_opt or play_opt.startswith("小") or "小" in play_opt:
        selection = "under"
    elif play_opt in ("1", "home", "主"):
        selection = "home"
    elif play_opt in ("2", "away", "客"):
        selection = "away"

    market_val = inner.get("marketValue") or inner.get("marketValues") or inner.get("hv")
    line = None
    if market_val is not None:
        try:
            from app.services.bookmakers.plugins.ob.odds import parse_asian_line

            line = parse_asian_line(market_val)
        except Exception:
            try:
                line = float(str(market_val).replace("/", "."))
            except Exception:
                line = None

    match_info = (
        inner.get("matchInfo")
        or inner.get("matchName")
        or f"{inner.get('homeName') or ''} v {inner.get('awayName') or ''}"
    )
    ts = inner.get("betTime") or inner.get("createTime") or inner.get("ts")
    created = None
    if ts:
        try:
            tsi = int(ts)
            if tsi > 1e12:
                tsi = tsi // 1000
            created = datetime.fromtimestamp(tsi, tz=timezone.utc).isoformat()
        except Exception:
            created = str(ts)

    return {
        "external_bet_id": str(order_no),
        "provider": "OB体育",
        "provider_code": "ob",
        "status": status,
        "stake": stake_f,
        "odds": odds_f,
        "actual_payout": payout_f,
        "selection": selection,
        "bet_type": "total",
        "line": line,
        "match_info": str(match_info or "").strip(),
        "created_at": created,
        "raw_status": inner.get("betStatus") or inner.get("orderStatus"),
    }


async def fetch_ob_orders(*, page, session_token: str = "", days: int = 3) -> dict:
    """在已打开的开云盘口页内拉取近期注单。"""
    token = sanitize_token(session_token)
    sport_token = ""
    cuid = ""
    api_host = "https://api.937kddt.com"

    # 挖 sport token
    try:
        for fr in list(getattr(page, "frames", []) or []) + [page]:
            try:
                u = getattr(fr, "url", "") or ""
            except Exception:
                u = ""
            if "token=" in u:
                qs = parse_qs(urlparse(u).query)
                st = (qs.get("token") or [""])[0]
                sid = (qs.get("sessionId") or [""])[0]
                if st:
                    sport_token = st
                mm = re.match(r"^(\d{15,22})", sid or "")
                if mm:
                    cuid = mm.group(1)
            try:
                st2 = await fr.evaluate(
                    """() => {
                      try {
                        return localStorage.getItem('token')
                          || sessionStorage.getItem('token') || '';
                      } catch (e) { return ''; }
                    }"""
                )
                if st2 and not sport_token:
                    sport_token = str(st2)
            except Exception:
                pass
    except Exception:
        pass

    if not sport_token and token:
        sport_token = token
    if not sport_token:
        return {"ok": False, "message": "体育 token 缺失", "orders": []}

    # 发现 API host - 搜索所有 frames 的 performance entries
    try:
        all_frames = list(getattr(page, "frames", []) or []) + [page]
        for fr in all_frames:
            try:
                hosts = await fr.evaluate(
                    """() => {
                      const out = [];
                      try {
                        for (const e of (performance.getEntriesByType('resource')||[])) {
                          try {
                            // matchesPB 是赔率 API，它的 host 就是正确的 API host
                            if (/matchesPB|yewu.*\\/m\\/|betOrder/i.test(e.name)) {
                              const u = new URL(e.name);
                              if (out.indexOf(u.origin)<0) out.push(u.origin);
                            }
                          } catch (e) {}
                        }
                        // 也检查一般 API 请求
                        for (const e of (performance.getEntriesByType('resource')||[])) {
                          try {
                            const u = new URL(e.name);
                            if (/api\\.|rccg|kaiyun/i.test(u.href) && out.indexOf(u.origin)<0)
                              out.push(u.origin);
                          } catch (e) {}
                        }
                      } catch (e) {}
                      return out.slice(0, 6);
                    }"""
                )
                if isinstance(hosts, list) and hosts:
                    api_host = hosts[0]  # matchesPB host 排在前面
                    break
            except Exception:
                pass
    except Exception:
        pass

    now_ms = int(time.time() * 1000)
    start_ms = now_ms - max(1, int(days)) * 86400 * 1000
    bodies = [
        # 已结算（orderStatus=1, timeType=5=今天）
        {"page": 1, "size": 50, "orderStatus": 1, "outright": None, "timeType": 5, "selected": 0},
        # 未结算（orderStatus=0）
        {"page": 1, "size": 50, "orderStatus": 0, "outright": None, "timeType": 5, "selected": 0},
        # 全部（orderStatus=2, timeType=1=最近）
        {"page": 1, "size": 50, "orderStatus": 2, "outright": None, "timeType": 1, "selected": 0},
        # 已结算 + 更长时间范围
        {"page": 1, "size": 50, "orderStatus": 1, "outright": None, "timeType": 1, "selected": 0},
    ]

    # GET 请求的 query 参数（参考 countUnsettleTickets）
    get_params = f"outright=0&pageNo=1&pageSize=50&timeType=1&beginTime={start_ms}&endTime={now_ms}&t={now_ms}"

    tried = []
    orders: list[dict] = []

    # ★ 先确保体育前端 iframe 已加载（后续所有方案都依赖页面认证状态）
    venue_url = "https://www.hflqfu.vip:9013/game/sport/ob?enName=YBTY"
    try:
        current_url = page.url or ""
        if "game/sport" not in current_url:
            await page.goto(venue_url, wait_until="domcontentloaded", timeout=15000)
        # 等待体育前端 iframe 加载（最多 25 秒）
        for _ in range(25):
            has_zlshelves = any("zlshelves" in (getattr(fr, "url", "") or "") for fr in list(page.frames))
            if has_zlshelves:
                logger.info("OB 注单: 体育前端 iframe 已加载")
                break
            await asyncio.sleep(1)
        else:
            # iframe 没加载，尝试重新导航
            logger.info("OB 注单: iframe 未加载，重新导航 networkidle")
            await page.goto(venue_url, wait_until="networkidle", timeout=30000)
            await asyncio.sleep(5)
    except Exception as e:
        logger.warning("OB 注单: 导航失败: %s", e)

    # 方案 0: GET 请求（参考 countUnsettleTickets 的 GET 方式）
    for host in (api_host, "https://api.937kddt.com"):
        for path in _ORDER_PATHS:
            url = f"{host}{path}?{get_params}"
            try:
                resp = await page.request.get(
                    url,
                    headers={
                        "lang": "zh",
                        "requestid": sport_token,
                        "accept": "application/json",
                    },
                    timeout=15000,
                )
                text = await resp.text()
                tried.append({"host": host, "path": path, "code": None, "status": resp.status, "via": "get"})
                if resp.status == 200 and text:
                    import json as _json
                    try:
                        js_data = _json.loads(text)
                    except Exception:
                        js_data = {}
                    code = js_data.get("code") if isinstance(js_data, dict) else None
                    # 接受 code=0/200 或无 code 但有 data 的响应
                    has_valid_code = str(code) in ("0", "0000", "0000000", "200") or code in (0, 200) or code is None
                    if has_valid_code and isinstance(js_data, dict):
                        data = js_data.get("data")
                        data = _decode_gzip_data(data)
                        rows = []
                        if isinstance(data, list):
                            rows = data
                        elif isinstance(data, dict):
                            for k in ("list", "records", "orderList", "betList", "orderDetailRespList", "data", "rows", "pageList"):
                                if isinstance(data.get(k), list):
                                    rows = data[k]
                                    break
                        for row in rows:
                            if isinstance(row, dict) and isinstance(row.get("orderDetailList"), list):
                                for d in row["orderDetailList"]:
                                    n = _normalize_row(d if isinstance(d, dict) else {})
                                    if n:
                                        if not n.get("external_bet_id") and row.get("orderNo"):
                                            n["external_bet_id"] = str(row.get("orderNo"))
                                        orders.append(n)
                            else:
                                n = _normalize_row(row if isinstance(row, dict) else {})
                                if n:
                                    orders.append(n)
                        if orders:
                            seen = set()
                            uniq = []
                            for o in orders:
                                k = o["external_bet_id"]
                                if k in seen:
                                    continue
                                seen.add(k)
                                uniq.append(o)
                            return {"ok": True, "orders": uniq, "host": host, "path": path, "count": len(uniq), "via": "get"}
            except Exception:
                pass
    # 方案 A: 通过 page.on("response") 拦截页面自身的 API 响应
    # 先尝试提取页面 token 直接调用 API
    try:
        # 从页面 localStorage 提取 token
        page_token = None
        for fr in [page] + list(page.frames):
            try:
                fr_url = getattr(fr, "url", "") or ""
                if "hflqfu" in fr_url or "zlshelves" in fr_url or "about:blank" in fr_url:
                    for key in ["token", "userToken", "accessToken", "Authorization"]:
                        try:
                            val = await fr.evaluate(f"localStorage.getItem('{key}')")
                            if val and len(val) > 10:
                                page_token = val
                                logger.info("OB 注单: 从 localStorage[%s] 提取 token=%s...", key, page_token[:20])
                                break
                        except Exception:
                            pass
                    if page_token:
                        break
                    # 也尝试从 URL 参数提取
                    try:
                        val = await fr.evaluate("() => { const m = document.cookie.match(/token=([^;]+)/); return m ? m[1] : null; }")
                        if val and len(val) > 10:
                            page_token = val
                            logger.info("OB 注单: 从 cookie 提取 token=%s...", page_token[:20])
                            break
                    except Exception:
                        pass
            except Exception:
                pass

        # 如果有 token，直接调用 API
        if page_token:
            # 提取页面 cookies（全部，不过滤域名）
            cookie_str = ""
            try:
                cookies = await page.context.cookies()
                cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
                logger.info("OB 注单: 提取 cookies=%d 个, cookie_str[:100]=%s", len(cookies), cookie_str[:100])
            except Exception as e:
                logger.warning("OB 注单: 提取 cookies 失败: %s", e)

            for body in bodies:
                try:
                    url = f"https://api.937kddt.com/yewurecord/order/betRecord/getOrderListPB?t={now_ms}"
                    resp = await page.request.post(
                        url,
                        headers={
                            "content-type": "application/json",
                            "lang": "zh",
                            "cookie": cookie_str,
                            "requestid": page_token,
                        },
                        data=json.dumps(body),
                        timeout=15000,
                    )
                    text = await resp.text()
                    tried.append({"host": "api.937kddt.com", "path": "/yewurecord/order/betRecord/getOrderListPB", "code": None, "status": resp.status, "via": "token-post"})
                    logger.info("OB 注单 token-post: status=%d text[:300]=%s", resp.status, text[:300])
                    if resp.status == 200 and text:
                        try:
                            js_data = json.loads(text)
                        except Exception:
                            js_data = {}
                        code = js_data.get("code") if isinstance(js_data, dict) else None
                        logger.info("OB 注单 token-post: code=%s status=%d text[:300]=%s", code, resp.status, text[:300])
                        if isinstance(js_data, dict) and (str(code) in ("0", "0000", "0000000", "200") or code in (0, 200) or code is None):
                            data = js_data.get("data")
                            data = _decode_gzip_data(data)
                            logger.info("OB 注单 token-post: code=%s data_type=%s data[:200]=%s", code, type(data).__name__, str(data)[:200])
                            rows = []
                            if isinstance(data, list):
                                rows = data
                            elif isinstance(data, dict):
                                for k in ("list", "records", "orderList", "betList", "orderDetailRespList", "data", "rows", "pageList", "items"):
                                    if isinstance(data.get(k), list):
                                        rows = data[k]
                                        break
                            for row in rows:
                                if isinstance(row, dict) and isinstance(row.get("orderDetailList"), list):
                                    for d in row["orderDetailList"]:
                                        n = _normalize_row(d if isinstance(d, dict) else {})
                                        if n:
                                            if not n.get("external_bet_id") and row.get("orderNo"):
                                                n["external_bet_id"] = str(row.get("orderNo"))
                                            orders.append(n)
                                else:
                                    n = _normalize_row(row if isinstance(row, dict) else {})
                                    if n:
                                        orders.append(n)
                            if orders:
                                seen = set()
                                uniq = []
                                for o in orders:
                                    k = o["external_bet_id"]
                                    if k in seen:
                                        continue
                                    seen.add(k)
                                    uniq.append(o)
                                return {"ok": True, "orders": uniq, "host": "api.937kddt.com", "path": "getOrderListPB", "count": len(uniq), "via": "token-post"}
                except Exception as te:
                    logger.warning("OB 注单 token-post 单次异常: %s", te)
    except Exception as e:
        logger.warning("OB token-post 失败: %s", e)

    # 方案 A2: 通过 page.on("response") 拦截页面自身的 API 响应
    captured_data = {"text": None, "status": 0}

    try:
        def _on_resp(resp):
            try:
                if "getOrderListPB" in resp.url and resp.request.method == "POST":
                    captured_data["status"] = resp.status
                    # 同时捕获请求体
                    try:
                        captured_data["req_body"] = resp.request.post_data
                    except Exception:
                        pass
                    asyncio.ensure_future(_read_body(resp, captured_data))
            except Exception:
                pass

        async def _read_body(resp, store):
            try:
                body = await resp.text()
                store["text"] = body[:8000]
            except Exception:
                pass

        page.on("response", _on_resp)

        clicked = False
        frame_urls = []
        for fr in [page] + list(page.frames):
            try:
                frame_urls.append(getattr(fr, "url", "")[:80])
            except Exception:
                pass
        logger.info("OB 注单: frames=%s", frame_urls)

        for fr in [page] + list(page.frames):
            try:
                for sel in ["text=投注记录", "text=注单"]:
                    loc = fr.locator(sel).first
                    if await loc.count() > 0:
                        await loc.click(timeout=3000)
                        clicked = True
                        logger.info("OB 注单: 点击 '%s' 触发 API", sel)
                        # 等 2 秒后点击"已结算"tab
                        await asyncio.sleep(2)
                        for tab_sel in ["text=已结算", "text=全部"]:
                            try:
                                tab = fr.locator(tab_sel).first
                                if await tab.count() > 0:
                                    await tab.click(timeout=3000)
                                    logger.info("OB 注单: 点击 tab '%s'", tab_sel)
                                    break
                            except Exception:
                                pass
                        break
                if clicked:
                    break
            except Exception:
                pass

        if not clicked:
            for fr in list(page.frames):
                try:
                    fr_url = getattr(fr, "url", "") or ""
                    if "zlshelves" in fr_url or "user-pc-new" in fr_url:
                        for hash_path in ["#/betHistory", "#/record", "#/orders", "#/personal/betHistory", "#/personal/record"]:
                            try:
                                await fr.evaluate(f"window.location.hash = '{hash_path}'")
                                await asyncio.sleep(2)
                                if captured_data.get("text"):
                                    clicked = True
                                    logger.info("OB 注单: 导航到 %s 触发 API", hash_path)
                                    break
                            except Exception:
                                pass
                        break
                except Exception:
                    pass

        for _ in range(40):
            if captured_data.get("text"):
                # 接受任何 orderStatus 的响应（未结算/已结算）
                req_body = captured_data.get("req_body", "")
                if "getOrderListPB" in (req_body or ""):
                    break
            await asyncio.sleep(0.5)

        try:
            page.remove_listener("response", _on_resp)
        except Exception:
            pass

        logger.info("OB 注单: clicked=%s captured=%s status=%s", clicked, bool(captured_data.get("text")), captured_data.get("status"))

        if captured_data.get("text"):
            text = captured_data["text"]
            status = captured_data.get("status", 200)
            tried.append({"host": "api.937kddt.com", "path": "/yewurecord/order/betRecord/getOrderListPB", "code": None, "status": status, "via": "response-listener"})
            try:
                js_data = json.loads(text)
            except Exception:
                js_data = {}
            if isinstance(js_data, dict):
                code = js_data.get("code")
                data = js_data.get("data")
                data = _decode_gzip_data(data)
                logger.info("OB 注单 response-listener: code=%s data_type=%s req_body=%s data[:200]=%s", code, type(data).__name__, captured_data.get("req_body", "")[:300], str(data)[:200])
                if str(code) in ("0", "0000", "0000000", "200") or code in (0, 200) or code is None:
                    rows = []
                    if isinstance(data, list):
                        rows = data
                    elif isinstance(data, dict):
                        for k in ("list", "records", "orderList", "betList", "orderDetailRespList", "data", "rows", "pageList", "items"):
                            if isinstance(data.get(k), list):
                                rows = data[k]
                                break
                    for row in rows:
                        if isinstance(row, dict) and isinstance(row.get("orderDetailList"), list):
                            for d in row["orderDetailList"]:
                                n = _normalize_row(d if isinstance(d, dict) else {})
                                if n:
                                    if not n.get("external_bet_id") and row.get("orderNo"):
                                        n["external_bet_id"] = str(row.get("orderNo"))
                                    orders.append(n)
                        else:
                            n = _normalize_row(row if isinstance(row, dict) else {})
                            if n:
                                orders.append(n)
                    if orders:
                        seen = set()
                        uniq = []
                        for o in orders:
                            k = o["external_bet_id"]
                            if k in seen:
                                continue
                            seen.add(k)
                            uniq.append(o)
                        return {"ok": True, "orders": uniq, "host": "api.937kddt.com", "path": "getOrderListPB", "count": len(uniq), "via": "response-listener"}
    except Exception as e:
        logger.warning("OB response-listener 失败: %s", e)

    # 方案 B: 通过 page.request.post（原方式）
    for host in (api_host, "https://api.937kddt.com", "https://api.rccg5fz.com"):
        for path in _ORDER_PATHS:
            for body in bodies:
                url = f"{host}{path}?t={now_ms}"
                try:
                    resp = await page.request.post(
                        url,
                        headers={
                            "content-type": "application/json",
                            "lang": "zh",
                            "requestid": sport_token,
                            "accept": "application/json",
                        },
                        data=body,
                        timeout=20000,
                    )
                    text = await resp.text()

                    try:
                        js = json.loads(text) if text else {}
                    except Exception:
                        js = {}
                    code = js.get("code") if isinstance(js, dict) else None
                    tried.append({"host": host, "path": path, "code": code, "status": resp.status})
                    if not isinstance(js, dict):
                        continue
                    if str(code) not in ("0", "0000", "0000000", "200") and code not in (0, 200):
                        continue
                    data = js.get("data")
                    data = _decode_gzip_data(data)
                    logger.info("OB 注单 方案B: code=%s data_type=%s data[:200]=%s", code, type(data).__name__, str(data)[:200])
                    rows = []
                    if isinstance(data, list):
                        rows = data
                    elif isinstance(data, dict):
                        for k in (
                            "list", "records", "orderList", "betList",
                            "orderDetailRespList", "data", "rows",
                        ):
                            if isinstance(data.get(k), list):
                                rows = data[k]
                                break
                    for row in rows:
                        # 有的是 series → orderDetailList
                        if isinstance(row, dict) and isinstance(row.get("orderDetailList"), list):
                            for d in row["orderDetailList"]:
                                n = _normalize_row(d if isinstance(d, dict) else {})
                                if n:
                                    if not n.get("external_bet_id") and row.get("orderNo"):
                                        n["external_bet_id"] = str(row.get("orderNo"))
                                    orders.append(n)
                        else:
                            n = _normalize_row(row if isinstance(row, dict) else {})
                            if n:
                                orders.append(n)
                    if orders:
                        # 去重
                        seen = set()
                        uniq = []
                        for o in orders:
                            k = o["external_bet_id"]
                            if k in seen:
                                continue
                            seen.add(k)
                            uniq.append(o)
                        return {
                            "ok": True,
                            "orders": uniq,
                            "host": host,
                            "path": path,
                            "count": len(uniq),
                        }
                except Exception as e:
                    tried.append({"host": host, "path": path, "error": str(e)[:120]})
                    continue

    return {
        "ok": False,
        "message": "未能拉取开云注单列表（接口路径可能变更）",
        "orders": [],
        "tried": tried[:12],
    }
