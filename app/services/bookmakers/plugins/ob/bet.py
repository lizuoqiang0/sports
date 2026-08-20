"""
开云体育（YBTY）真实下单。

优先复用已保持的长连接 page（单浏览器策略），在页内调用 betOrder。
禁止默认再 launch 新 Chromium。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from decimal import Decimal
from typing import Optional
from urllib.parse import parse_qs, urlparse

from app.services.bookmakers.base import PlaceBetResult
from app.services.bookmakers.plugins.ob.odds import sanitize_token

logger = logging.getLogger(__name__)

_BET_PATHS = (
    "/yewu13/v1/betOrder/bet",
    "/yewu13/v1/betOrder/betSingle",
    "/yewu13/v1/betOrder/addBet",
    "/yewu11/v1/betOrder/bet",
    "/yewu11/v1/betOrder/betSingle",
)


def _decimal_to_ov(odds: float) -> str:
    try:
        return str(int(round(float(odds) * 100000)))
    except (TypeError, ValueError):
        return "0"


def selection_ref_from_odds(odds_data: dict, selection: str) -> Optional[dict]:
    if not isinstance(odds_data, dict):
        return None
    meta = odds_data.get("_ob") or {}
    sels = meta.get("selections") or {}
    ref = sels.get(selection)
    if not isinstance(ref, dict):
        return None
    out = dict(ref)
    out["mid"] = str(meta.get("mid") or out.get("mid") or "")
    csid = str(meta.get("csid") or out.get("csid") or "").strip()
    if not csid:
        return None
    out["csid"] = csid
    out["tid"] = str(meta.get("tid") or out.get("tid") or "")
    out["match_type"] = int(meta.get("match_type") or out.get("match_type") or 1)
    if not out.get("oid") or not out.get("hid") or not out.get("mid"):
        return None
    return out


async def place_ybty_bet(
    *,
    base_url: str,
    session_token: str,
    match_external_id: str,
    selection: str,
    odds: float,
    stake: Decimal,
    bet_type: str = "total",
    odds_data: Optional[dict] = None,
    headed: bool = False,
    page=None,
    allow_launch: bool = False,
) -> PlaceBetResult:
    """
    allow_launch=False（默认）：无 page 时直接失败，禁止另开窗口。
    """
    token = sanitize_token(session_token)
    if not token or not base_url:
        return PlaceBetResult(ok=False, message="缺少 OB 会话，请先在站点配置验证登录")

    ref = selection_ref_from_odds(odds_data or {}, selection)
    if not ref:
        return PlaceBetResult(
            ok=False,
            message="盘口缺少真实投注参数(oid/hid)，请先「同步全部」刷新赔率后再下单",
            balance_after=Decimal("0"),
        )

    from app.services.bookmakers.odds_change import (
        ODDS_CHANGE_ACCEPT_FLOOR,
        decide_odds_change,
        odds_meaningfully_changed,
    )

    try:
        requested_odds = float(odds)
    except Exception:
        requested_odds = float(ODDS_CHANGE_ACCEPT_FLOOR)

    own_browser = False
    browser = None
    pw = None
    result = None
    h5_url: Optional[str] = None
    api_host = "https://api.rccg5fz.com"
    base = base_url.rstrip("/")

    try:
        page_closed = True
        try:
            page_closed = page is None or page.is_closed()
        except Exception:
            page_closed = True

        if page_closed:
            if not allow_launch:
                return PlaceBetResult(
                    ok=False,
                    message="无有效 OB 长连接浏览器，请先在站点配置验证登录（禁止另开窗口下单）",
                )
            from playwright.async_api import async_playwright

            pw = await async_playwright().start()
            browser = await pw.chromium.launch(
                headless=not headed,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            )
            context = await browser.new_context(
                ignore_https_errors=True,
                locale="zh-CN",
                viewport={"width": 1440, "height": 900},
            )
            page = await context.new_page()
            own_browser = True

        async def on_resp(resp):
            nonlocal h5_url, api_host
            try:
                url = resp.url
                if "venue/launch" in url:
                    body = await resp.json()
                    data = body.get("data") if isinstance(body, dict) else None
                    if isinstance(data, dict):
                        u = data.get("h5Url") or data.get("url") or data.get("activityUrl")
                        if u and "token=" in str(u):
                            h5_url = str(u)
                if "matchesPB" in url:
                    parsed = urlparse(url)
                    if parsed.scheme and parsed.hostname:
                        api_host = f"{parsed.scheme}://{parsed.hostname}"
                        if parsed.port:
                            api_host = f"{api_host}:{parsed.port}"
            except Exception:
                return

        page.on("response", on_resp)

        if own_browser:
            await page.goto(base + "/", wait_until="domcontentloaded", timeout=60000)
            await page.evaluate(
                "(t) => { try { localStorage.setItem('X-API-TOKEN', t); } catch (e) {} }",
                token,
            )
            await page.goto(base + "/game/sport", wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(2500)
            for sel in ("text=开云体育", "text=进入游戏", '[class*="sport"]'):
                try:
                    loc = page.locator(sel).first
                    if await loc.count() > 0:
                        await loc.click(timeout=2500)
                        await page.wait_for_timeout(2000)
                        break
                except Exception:
                    pass
            if not h5_url:
                await page.wait_for_timeout(4000)
            if not h5_url:
                return PlaceBetResult(ok=False, message="未能启动开云体育 H5，请重新验证登录")
            await page.goto(h5_url, wait_until="domcontentloaded", timeout=90000)
            await page.wait_for_timeout(4000)
        else:
            try:
                await page.evaluate(
                    "(t) => { try { localStorage.setItem('X-API-TOKEN', t); } catch (e) {} }",
                    token,
                )
            except Exception:
                pass

        # 从主页/iframe URL 挖体育 token（勿用综合站 X-API-TOKEN）
        def _ctx_from_url(u: str) -> tuple[str, str, str]:
            u = (u or "").strip()
            if not u or "token=" not in u:
                return "", "", ""
            qs = parse_qs(urlparse(u).query)
            st = (qs.get("token") or [""])[0]
            sid = (qs.get("sessionId") or [""])[0]
            cu = ""
            mm = re.match(r"^(\d{15,22})", sid or "")
            if mm:
                cu = mm.group(1)
            return (u if st else ""), st, cu

        sport_token = ""
        session_id = ""
        cuid = ""
        url_candidates: list[str] = []
        try:
            url_candidates.append(page.url or "")
        except Exception:
            pass
        try:
            for fr in list(getattr(page, "frames", []) or []):
                try:
                    fu = fr.url or ""
                except Exception:
                    fu = ""
                if fu:
                    url_candidates.append(fu)
        except Exception:
            pass
        try:
            sess_vu = ""
            from app.services.bookmakers.site_session import site_sessions

            sess = site_sessions.get(base)
            if sess:
                sess_vu = str(getattr(sess, "venue_url", "") or "")
            if sess_vu:
                url_candidates.append(sess_vu)
        except Exception:
            pass
        for u in url_candidates:
            hu, st, cu = _ctx_from_url(u)
            if st:
                if not h5_url:
                    h5_url = hu
                sport_token = st
                cuid = cu or cuid
                try:
                    session_id = (parse_qs(urlparse(hu or u).query).get("sessionId") or [""])[0]
                except Exception:
                    pass
                break
        if h5_url and not sport_token:
            _, sport_token, cuid2 = _ctx_from_url(h5_url)
            cuid = cuid or cuid2
            session_id = (parse_qs(urlparse(h5_url).query).get("sessionId") or [""])[0]
        if not cuid:
            m = re.match(r"^(\d{15,22})", session_id or "")
            if m:
                cuid = m.group(1)
        if not sport_token:
            # 仅在 H5/iframe 取 token，避免门户壳会员 token
            for fr in list(getattr(page, "frames", []) or []):
                try:
                    fu = (fr.url or "").lower()
                except Exception:
                    fu = ""
                if not any(x in fu for x in ("zlshelves", "yewu", "app-h5", "token=", "kaiyun")):
                    continue
                try:
                    sport_token = await fr.evaluate(
                        """() => {
                          try {
                            return localStorage.getItem('token')
                              || sessionStorage.getItem('token')
                              || '';
                          } catch (e) { return ''; }
                        }"""
                    )
                except Exception:
                    sport_token = ""
                if sport_token:
                    break
        if not sport_token:
            return PlaceBetResult(ok=False, message="体育 token 缺失，请重新验证并进入开云盘口")

        # 从页内近期请求推断 API host（matchesPB 同源）
        discover_js = """() => {
          const hosts = [];
          const push = (u) => {
            try {
              const x = new URL(u, location.href);
              if (/yewu|matchesPB|betOrder|rccg|zlshelves|kaiyun/i.test(x.href)) {
                const h = x.origin;
                if (hosts.indexOf(h) < 0) hosts.push(h);
              }
            } catch (e) {}
          };
          try {
            const ents = performance.getEntriesByType('resource') || [];
            for (const e of ents) push(e.name || '');
          } catch (e) {}
          try { push(location.href); } catch (e) {}
          return hosts.slice(0, 8);
        }"""
        api_hosts: list[str] = []
        for fr in [page] + list(getattr(page, "frames", []) or []):
            try:
                found = await fr.evaluate(discover_js)
                if isinstance(found, list):
                    for h in found:
                        hs = str(h or "").rstrip("/")
                        if hs and hs not in api_hosts:
                            api_hosts.append(hs)
            except Exception:
                continue
        if api_host and api_host not in api_hosts:
            api_hosts.append(api_host)
        for pref in ("https://api.937kddt.com", "https://api.rccg5fz.com"):
            if pref not in api_hosts:
                api_hosts.append(pref)
        # 真实 API 域名优先
        api_hosts = sorted(
            api_hosts,
            key=lambda h: (0 if "api." in h.lower() else 1, 0 if "937kddt" in h else 1, h),
        )[:4]

        mid = str(ref.get("mid") or "").strip()
        if not mid:
            raw_ext = str(match_external_id or "")
            mid = raw_ext.split(":")[-1].strip() if ":" in raw_ext else raw_ext.strip()
        if not mid:
            return PlaceBetResult(ok=False, message="缺少赛事 mid，请先同步滚球盘口")

        # 下单前用 matchesPB 刷新 oid/hid/ov，避免「盘口失效」
        try:
            from app.services.bookmakers.plugins.ob.odds import (
                _moneyline_from_hps,
                _spread_from_hps,
                _total_from_hps,
                decode_pb_data,
            )

            refresh_hosts = [h for h in api_hosts if "api." in h.lower()] or api_hosts[:1]
            refresh_js = """async ({ hosts, sportToken, cuid, specs }) => {
              const headers = { 'content-type': 'application/json', 'lang': 'zh', 'requestid': sportToken };
              for (const host of hosts) {
                for (const spec of specs) {
                  try {
                    const resp = await fetch(host + '/yewu11/v1/m/matchesPB?t=' + Date.now(), {
                      method: 'POST',
                      headers,
                      body: JSON.stringify({
                        cuid: cuid || '', euid: String(spec.euid || '40053'),
                        type: Number(spec.type || 1), sort: 1,
                        device: 'v2_h5_st', hpsFlag: 1, category: 1,
                      }),
                      credentials: 'omit',
                    });
                    const json = await resp.json();
                    if (json && json.data) return { host, json };
                  } catch (e) {}
                }
              }
              return null;
            }"""
            freshest = None
            for fr in list(getattr(page, "frames", []) or []) + [page]:
                try:
                    fu = (getattr(fr, "url", "") or "").lower()
                except Exception:
                    fu = ""
                if fr is not page and not any(x in fu for x in ("zlshelves", "yewu", "app-h5", "token=")):
                    continue
                try:
                    freshest = await asyncio.wait_for(
                        fr.evaluate(
                            refresh_js,
                            {
                                "hosts": refresh_hosts,
                                "sportToken": sport_token,
                                "cuid": cuid or "",
                                "specs": [{"euid": "40053", "type": 1}, {"euid": "40203", "type": 1}],
                            },
                        ),
                        timeout=15.0,
                    )
                except Exception:
                    freshest = None
                if freshest:
                    break
            if isinstance(freshest, dict) and freshest.get("json"):
                rows = decode_pb_data((freshest.get("json") or {}).get("data")) or []
                for row in rows if isinstance(rows, list) else []:
                    if not isinstance(row, dict):
                        continue
                    if str(row.get("mid") or "") != mid:
                        continue
                    hps = row.get("hps") or []
                    csid = str(row.get("csid") or ref.get("csid") or "1")
                    tid = str(row.get("tid") or ref.get("tid") or "")
                    try:
                        mtype = int(ref.get("match_type") or 1)
                    except Exception:
                        mtype = 1
                    remote = None
                    bt = str(bet_type or "total").lower()
                    # 小球盘口别名归一（与 site_bet/market_recommend 对齐）：
                    # 否则 "ou"/"totals" 会静默落入独赢刷新分支拿不到 oid/hid
                    if bt in ("total", "totals", "ou", "大小"):
                        remote = _total_from_hps(hps, mid=mid, csid=csid, tid=tid, match_type=mtype)
                    elif bt in ("spread", "ah", "handicap", "asian_handicap"):
                        remote = _spread_from_hps(hps, mid=mid, csid=csid, tid=tid, match_type=mtype)
                    else:
                        remote = _moneyline_from_hps(hps, mid=mid, csid=csid, tid=tid, match_type=mtype)
                    if remote and isinstance(remote.odds_data, dict):
                        new_ref = selection_ref_from_odds(remote.odds_data, selection)
                        if new_ref:
                            ref = new_ref
                            try:
                                odds = float(remote.odds_data.get(selection) or odds)
                            except Exception:
                                pass
                            # 滚球比分基准：S1 当前比分
                            try:
                                from app.services.bookmakers.plugins.ob.odds import (
                                    _parse_msc_score,
                                )

                                hs, aws = _parse_msc_score(row.get("msc"))
                                ref["scoreBenchmark"] = f"{int(hs)}:{int(aws)}"
                            except Exception:
                                pass
                            if remote.total and not ref.get("hv"):
                                # 2.25 → 2/2.5 近似（优先 hl.hv）
                                t = float(remote.total)
                                if abs(t * 4 - round(t * 4)) < 1e-6 and abs(t % 0.5 - 0.25) < 1e-6:
                                    lo = int(t) if t >= 0 else int(t) - 1
                                    ref["hv"] = f"{lo}/{lo + 0.5}"
                                else:
                                    ref["hv"] = str(t).rstrip("0").rstrip(".")
                            logger.info(
                                "OB bet refreshed mid=%s sel=%s odds=%s oid=%s hv=%s",
                                mid, selection, odds, ref.get("oid"), ref.get("hv"),
                            )
                    break
        except Exception as e:
            logger.warning("OB bet refresh matchesPB skipped: %s", e)

        # 赔率变动：≥1.7 自动接受；<1.7 放弃
        ok_chg, why_chg, use_odds = decide_odds_change(requested_odds, odds)
        if not ok_chg:
            logger.info("OB bet abort odds-change: %s", why_chg)
            return PlaceBetResult(ok=False, message=why_chg)
        if use_odds is not None:
            odds = float(use_odds)
        logger.info("OB bet odds-change policy: %s (use=%s)", why_chg, odds)

        ov = str(ref.get("ov") or _decimal_to_ov(odds))
        hid = str(ref.get("hid") or "").strip()
        oid = str(ref.get("oid") or "").strip()
        try:
            ov_int = int(float(ov))
        except Exception:
            ov_int = int(round(float(odds) * 100000))
        # 刷新后 ov 可能仍是旧值，按当前亚洲盘重算
        try:
            ov_from_eu = int(round(float(odds) * 100000))
            if abs(ov_from_eu - ov_int) > 50:
                ov_int = ov_from_eu
                ov = str(ov_int)
                ref["ov"] = ov
        except Exception:
            pass

        def _num_or_str(v: str):
            s = str(v or "").strip()
            if s.isdigit():
                try:
                    return int(s)
                except Exception:
                    return s
            return s

        mid_v = _num_or_str(mid)
        hid_v = _num_or_str(hid)
        oid_v = _num_or_str(oid)
        market_value = str(ref.get("hv") or ref.get("marketValue") or "").strip()
        # odds 字段必须用 ov 整数（如 194000），不能发亚赔小数 1.94
        detail = {
            "marketId": str(hid),
            "matchInfoId": str(mid),
            "matchId": str(mid),
            "odds": ov_int,
            "oddFinally": str(odds),
            "playId": str(ref.get("hpid") or "1"),
            "playOptionsId": str(oid),
            "placeNum": int(ref.get("place_num") or ref.get("placeNum") or 0),
            "matchType": int(ref.get("match_type") or 1),
            "sportId": int(str(ref.get("csid") or "1") or "1")
            if str(ref.get("csid") or "1").isdigit()
            else _num_or_str(str(ref.get("csid") or "1")),
            "tournamentId": str(ref.get("tid") or ""),
            "betAmount": float(stake),
            "dataSource": "SR",
            "marketTypeFinally": "EU",
            "tradeType": 0,
        }
        if market_value:
            detail["marketValue"] = market_value
        score_bm = str(ref.get("scoreBenchmark") or ref.get("score") or "").strip()
        if score_bm:
            detail["scoreBenchmark"] = score_bm
        # 大小球：补 playOptions（over 同样需要，OB 接口要求显式方向）
        sel_l = str(selection or "").lower()
        if sel_l == "under":
            detail["playOptions"] = "Under"
        elif sel_l == "over":
            detail["playOptions"] = "Over"
        logger.info(
            "OB bet body mid=%s hid=%s oid=%s hpid=%s stake=%s odds=%s ov=%s mv=%s",
            mid, hid, oid, ref.get("hpid"), stake, odds, ov_int, market_value,
        )

        def _make_body(det: dict, accept: int = 1) -> dict:
            return {
                "acceptOdds": accept,
                "tenantId": 1,
                "deviceType": 2,
                "currencyCode": "CNY",
                "device": "v2_h5_st",
                "cuid": cuid or "",
                "seriesOrders": [
                    {
                        "seriesSum": 1,
                        "seriesType": 1,
                        "seriesValues": "1",
                        "fullBet": 0,
                        "orderDetailList": [det],
                    }
                ],
            }

        # 已确认变动且 ≥1.7：acceptOdds=2；未变动用 1，拒绝后再按地板价决定
        # 成功后勿再连发，否则会重复扣款
        accept_flag = (
            2
            if odds_meaningfully_changed(requested_odds, odds)
            and float(odds) + 1e-9 >= float(ODDS_CHANGE_ACCEPT_FLOOR)
            else 1
        )
        body_variants = [_make_body(detail, accept_flag)]

        # 用 Playwright request（带 cookie、无 CORS）提交
        result = None
        all_tried: list = []
        ok_codes = {0, "0", 200, "200", "0000", "0000000"}
        path = "/yewu13/v1/betOrder/bet"
        req_hosts = [h for h in api_hosts if "937kddt" in h.lower()] or [
            h for h in api_hosts if "api." in h.lower()
        ] or api_hosts[:1]

        async def _post_once(host: str, body_try: dict) -> tuple[dict | None, dict]:
            url = f"{host}{path}?t={int(time.time() * 1000)}"
            try:
                resp = await page.request.post(
                    url,
                    headers={
                        "content-type": "application/json",
                        "lang": "zh",
                        "requestid": sport_token,
                        "accept": "application/json",
                    },
                    data=body_try,
                    timeout=20000,
                )
                text = await resp.text()
                try:
                    json_body = json.loads(text) if text else None
                except Exception:
                    json_body = None
                code = msg = None
                if isinstance(json_body, dict):
                    code = json_body.get("code", json_body.get("statusCode"))
                    msg = json_body.get("msg") or json_body.get("message")
                trial = {
                    "host": host,
                    "path": path,
                    "via": "page.request",
                    "status": resp.status,
                    "code": code,
                    "msg": msg,
                    "data": (json_body or {}).get("data") if isinstance(json_body, dict) else None,
                    "raw": (text or "")[:500],
                }
                return (json_body if isinstance(json_body, dict) else None), trial
            except Exception as e:
                return None, {
                    "host": host,
                    "path": path,
                    "via": "page.request",
                    "error": f"{type(e).__name__}: {e}",
                }

        def _patch_from_reject(det: dict, payload) -> dict | None:
            """盘口失效时，API 常在 data[] 回传最新盘口模板；优先整包回投。"""
            rows = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(rows, list) or not rows:
                return None
            first = rows[0] if isinstance(rows[0], dict) else None
            if not first:
                return None
            info = first.get("data") if isinstance(first.get("data"), dict) else first
            if not isinstance(info, dict):
                return None
            # 以服务端模板为主，避免漏字段 / odds 小数格式错误
            skip = {
                "playName", "playOptionName", "matchInfo", "matchName", "sportName",
                "teamName", "excellentOddsBet", "tips", "traceId", "ts", "msg", "code",
            }
            out = {k: v for k, v in info.items() if k not in skip and v is not None}
            out["betAmount"] = float(det.get("betAmount") or stake)
            if info.get("odds") is not None:
                try:
                    out["odds"] = int(float(info["odds"]))
                except Exception:
                    out["odds"] = info["odds"]
            if info.get("oddFinally") is not None:
                out["oddFinally"] = str(info["oddFinally"])
            if info.get("matchId") is not None:
                out["matchId"] = str(info["matchId"])
                out["matchInfoId"] = str(info["matchId"])
            if info.get("marketId") is not None:
                out["marketId"] = str(info["marketId"])
            if info.get("playOptionsId") is not None:
                out["playOptionsId"] = str(info["playOptionsId"])
            return out

        def _order_no(payload: dict | None) -> str:
            if not isinstance(payload, dict):
                return ""
            data_body = payload.get("data")
            keys = ("orderNo", "orderId", "betOrderNo", "orderNoList")

            def _from_dict(d: dict) -> str:
                for k in keys:
                    v = d.get(k)
                    if isinstance(v, list) and v:
                        return str(v[0] or "")
                    if v:
                        return str(v)
                # 开云成功体：data.orderDetailRespList[].orderNo
                for item in d.get("orderDetailRespList") or []:
                    if isinstance(item, dict):
                        on = _from_dict(item)
                        if on:
                            return on
                return ""

            if isinstance(data_body, dict):
                on = _from_dict(data_body)
                if on:
                    return on
                # 无 orderNo 时用 globalId 兜底（仍算已受理）
                gid = str(data_body.get("globalId") or "").strip()
                if gid:
                    return f"ob-global:{gid}"
            if isinstance(data_body, list):
                for row in data_body:
                    if not isinstance(row, dict):
                        continue
                    row_code = row.get("code")
                    if row_code is not None and row_code != "" and row_code not in ok_codes:
                        continue
                    inner = row.get("data") if isinstance(row.get("data"), dict) else row
                    if isinstance(inner, dict):
                        on = _from_dict(inner)
                        if on:
                            return on
            return ""

        def _is_success(payload: dict | None) -> bool:
            if not isinstance(payload, dict):
                return False
            code = payload.get("code")
            top_ok = code in ok_codes or payload.get("success") is True
            if not top_ok:
                return False
            # 0000000 + orderDetailRespList / globalId 即成功
            return bool(_order_no(payload))

        for host in req_hosts[:1]:
            for body_try in list(body_variants):
                json_body, trial = await _post_once(host, body_try)
                all_tried.append(trial)
                if not json_body:
                    continue
                code = json_body.get("code")
                logger.info(
                    "OB bet resp host=%s code=%s order=%s raw=%s",
                    host, code, _order_no(json_body), str(json_body)[:500],
                )
                if _is_success(json_body):
                    result = {"ok": True, "path": path, "host": host, "json": json_body, "tried": all_tried}
                    logger.info(
                        "OB bet ok via page.request host=%s order=%s",
                        host, _order_no(json_body),
                    )
                    break
                if code in ok_codes or json_body.get("success") is True:
                    logger.warning(
                        "OB bet code-ok but no orderNo host=%s code=%s json=%s",
                        host, code, str(json_body)[:500],
                    )
                # 盘口失效：用回传最新模板重试；变动赔率须 ≥1.7 才接受
                if str(code) in ("0402008", "0402012"):
                    patched = _patch_from_reject(detail, json_body)
                    if patched:
                        live_od = None
                        try:
                            live_od = float(patched.get("oddFinally") or 0) or None
                        except Exception:
                            live_od = None
                        if live_od is None and patched.get("odds") is not None:
                            try:
                                ov_raw = float(patched.get("odds"))
                                live_od = ov_raw / 100000.0 if ov_raw > 100 else ov_raw
                            except Exception:
                                live_od = None
                        ok2, why2, use2 = decide_odds_change(requested_odds, live_od)
                        if not ok2:
                            logger.info("OB bet abort after market reject: %s", why2)
                            return PlaceBetResult(ok=False, message=why2)
                        if use2 is not None:
                            patched["oddFinally"] = str(use2)
                            try:
                                patched["odds"] = int(round(float(use2) * 100000))
                            except Exception:
                                pass
                        acc = 2 if (use2 or 0) + 1e-9 >= float(ODDS_CHANGE_ACCEPT_FLOOR) else 1
                        logger.info(
                            "OB bet retry with server market mid=%s oid=%s odds=%s mv=%s ds=%s policy=%s",
                            patched.get("matchId"),
                            patched.get("playOptionsId"),
                            patched.get("odds"),
                            patched.get("marketValue"),
                            patched.get("dataSource"),
                            why2,
                        )
                        json2, trial2 = await _post_once(host, _make_body(patched, acc))
                        all_tried.append(trial2)
                        if json2:
                            logger.info(
                                "OB bet retry-resp host=%s acc=%s code=%s order=%s raw=%s",
                                host, acc, json2.get("code"), _order_no(json2), str(json2)[:400],
                            )
                        if _is_success(json2):
                            result = {
                                "ok": True,
                                "path": path,
                                "host": host,
                                "json": json2,
                                "tried": all_tried,
                            }
                            logger.info(
                                "OB bet ok after market refresh host=%s order=%s",
                                host, _order_no(json2),
                            )
                            break
            if result and result.get("ok"):
                break
        if result is None:
            result = {"ok": False, "tried": all_tried}
    except Exception as e:
        logger.exception("place_ybty_bet failed")
        return PlaceBetResult(ok=False, message=f"下单异常: {e}")
    finally:
        if own_browser:
            try:
                if browser:
                    await browser.close()
            except Exception:
                pass
            try:
                if pw:
                    await pw.stop()
            except Exception:
                pass

    if not isinstance(result, dict):
        return PlaceBetResult(ok=False, message="下单无响应")

    if result.get("ok"):
        order_id = ""
        data = (result.get("json") or {}).get("data")
        if isinstance(data, dict):
            order_id = str(
                data.get("orderNo")
                or data.get("orderId")
                or data.get("betOrderNo")
                or ""
            )
            if not order_id:
                for item in data.get("orderDetailRespList") or []:
                    if isinstance(item, dict) and item.get("orderNo"):
                        order_id = str(item["orderNo"])
                        break
            if not order_id and isinstance(data.get("orderNoList"), list) and data["orderNoList"]:
                order_id = str(data["orderNoList"][0] or "")
            if not order_id and data.get("globalId"):
                order_id = f"ob-global:{data.get('globalId')}"
        elif isinstance(data, list) and data:
            first = data[0] if isinstance(data[0], dict) else {}
            inner = first.get("data") if isinstance(first.get("data"), dict) else first
            if isinstance(inner, dict):
                order_id = str(
                    inner.get("orderNo") or inner.get("orderId") or inner.get("betOrderNo") or ""
                )
                if not order_id:
                    for item in inner.get("orderDetailRespList") or []:
                        if isinstance(item, dict) and item.get("orderNo"):
                            order_id = str(item["orderNo"])
                            break
        if not order_id:
            return PlaceBetResult(ok=False, message="站点返回成功但无注单号，已拒绝记本地单")
        bal_after = Decimal("0")
        # 对标平博：下单成功后读页内余额，供扣款校验 / 账户同步
        try:
            from app.services.bookmakers.site_bet import _read_balance_from_page

            if page is not None and not own_browser:
                await page.wait_for_timeout(1200)
                bal_after = await _read_balance_from_page(page)
        except Exception:
            bal_after = Decimal("0")
        return PlaceBetResult(
            ok=True,
            message="已提交至开云体育",
            external_bet_id=order_id,
            balance_after=bal_after,
        )

    tried = result.get("tried") or []
    msgs = []
    for t in tried[-3:]:
        if t.get("msg"):
            msgs.append(str(t["msg"]))
        elif t.get("error"):
            msgs.append(str(t["error"]))
        elif t.get("raw"):
            msgs.append(str(t["raw"])[:120])
    detail = "；".join(msgs) if msgs else "体育站拒绝或接口路径变更"
    logger.warning("OB bet failed tried=%s", tried)
    return PlaceBetResult(ok=False, message=f"真实下单失败: {detail}")
