"""
平博（Asia Compact）真实盘口拉取。

对齐 OB matchesPB：页内 fetch /sports-service/sv/compact/events
（足球 sp=29 + 篮球 sp=4，mk=2 滚球），禁止切页双球类 DOM。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse

from app.services.bookmakers.base import RemoteMatch, RemoteOdds
from app.services.bookmakers.plugins.ob.odds import is_virtual_match
from app.services.odds_domain import normalize_odds_data_to_european

logger = logging.getLogger(__name__)

SUPPORTED_SPORTS = {"football", "basketball"}
SPORT_IDS = {
    29: "football",
    4: "basketball",
}

# compact/events 查询：mk=2 Live，btg=1 HDP+OU，ot=1 亚盘风格小数
_FETCH_JS = """
async ({ liveOnly, sportIds, origins }) => {
  const mk = liveOnly ? 2 : 3;
  const lFilter = liveOnly ? 2 : 0;
  // 勿乱加 Authorization：错误 Bearer 会导致 sports-service 直接 401
  const headers = { accept: 'application/json, text/plain, */*' };

  const originsTry = [];
  const push = (o) => {
    if (!o || typeof o !== 'string') return;
    const x = o.replace(/\\/+$/, '');
    if (x && originsTry.indexOf(x) < 0) originsTry.push(x);
  };
  try { push(location.origin); } catch (e) {}
  for (const o of (origins || [])) push(o);

  const discovered = [];
  try {
    for (const e of performance.getEntriesByType('resource')) {
      const n = e.name || '';
      if (/sports-service\\/sv\\/compact\\/events/i.test(n)) discovered.push(n.split('#')[0]);
    }
  } catch (e) {}

  const payloads = [];
  const debug = [];
  const seenBody = new Set();
  const pushPayload = (j) => {
    if (!j || typeof j !== 'object') return;
    // compact 有效响应含 l 数组
    if (!Array.isArray(j.l) && !Array.isArray(j.n)) return;
    let key = '';
    try { key = String((j.l || []).length) + ':' + JSON.stringify(j).slice(0, 160); } catch (e) { key = String(Math.random()); }
    if (seenBody.has(key)) return;
    seenBody.add(key);
    payloads.push(j);
  };

  const fetchJson = async (url) => {
    try {
      const r = await fetch(url, { credentials: 'include', headers, method: 'GET' });
      if (!r.ok) {
        debug.push({ url: String(url).slice(0, 120), status: r.status });
        return null;
      }
      const j = await r.json();
      if (j && typeof j === 'object') return j;
      debug.push({ url: String(url).slice(0, 120), status: r.status, empty: true });
      return null;
    } catch (e) {
      debug.push({ url: String(url).slice(0, 120), err: String(e).slice(0, 80) });
      return null;
    }
  };

  for (const u of discovered.slice(0, 8)) {
    const j = await fetchJson(u);
    if (j) pushPayload(j);
  }

  for (const origin of originsTry.slice(0, 3)) {
    for (const sp of sportIds) {
      const qs = [
        'sp=' + sp,
        'lg=',
        'ev=',
        'mk=' + mk,
        'btg=1',
        'ot=1',
        'd=',
        'o=0',
        'l=' + lFilter,
        'v=',
        'lv=',
        'me=0',
        'more=false',
        'tm=0',
        'pa=0',
        'c=Others',
        'pn=-1',
        'cl=-1',
        'hle=true',
        'inl=false',
        'pv=1',
        'ic=false',
        'ice=false',
        'withCredentials=true',
        'lang=zh_CN',
      ].join('&');
      const url = origin + '/sports-service/sv/compact/events?' + qs;
      const j = await fetchJson(url);
      if (j) pushPayload(j);
    }
    if (payloads.length >= sportIds.length) break;
  }
  return { n: payloads.length, origins: originsTry.slice(0, 3), payloads, debug: debug.slice(0, 8) };
}
"""


def _as_float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _pick_main_spread(lines: list) -> Optional[list]:
    """p0[0] 让球线：优先 alt=0（主盘）。"""
    best = None
    for row in lines or []:
        if not isinstance(row, list) or len(row) < 5:
            continue
        alt = row[8] if len(row) > 8 else 0
        if alt == 0:
            return row
        if best is None:
            best = row
    return best


def _pick_main_total(lines: list) -> Optional[list]:
    """p0[1] 大小线：优先 alt=0。"""
    best = None
    for row in lines or []:
        if not isinstance(row, list) or len(row) < 4:
            continue
        alt = row[5] if len(row) > 5 else 0
        if alt == 0:
            return row
        if best is None:
            best = row
    return best


def _ms_to_iso(ms: Any) -> str:
    try:
        n = int(ms)
        if n > 1_000_000_000_000:
            n = n // 1000
        if n <= 0:
            return ""
        return datetime.fromtimestamp(n, tz=timezone.utc).isoformat()
    except Exception:
        return ""


def _odds_from_period0(p0: list, *, mid: str, sport_id: int) -> list[RemoteOdds]:
    out: list[RemoteOdds] = []
    if not isinstance(p0, list) or len(p0) < 3:
        return out

    # 让球
    sp_row = _pick_main_spread(p0[0] if isinstance(p0[0], list) else [])
    if sp_row:
        home_od = _as_float(sp_row[3])
        away_od = _as_float(sp_row[4])
        line = _as_float(sp_row[0])
        if home_od and away_od and line is not None:
            sel_id = str(sp_row[7] if len(sp_row) > 7 else "")
            od = RemoteOdds(
                bet_type="spread",
                odds_data={
                    "home": home_od,
                    "away": away_od,
                    "_site": {
                        "bet_type": "spread",
                        "line": float(line),
                        "mid": mid,
                        "sport_id": str(sport_id),
                        "site_code": "pinnacle",
                        "selections": {
                            "home": {"id": sel_id, "oid": sel_id, "price": home_od, "name": "home"},
                            "away": {"id": sel_id, "oid": sel_id, "price": away_od, "name": "away"},
                        },
                    },
                },
                spread=float(line),
            )
            out.append(od)

    # 大小
    tot_row = _pick_main_total(p0[1] if isinstance(p0[1], list) else [])
    if tot_row:
        over = _as_float(tot_row[2])
        under = _as_float(tot_row[3])
        # points 优先数值位，其次展示串
        line = _as_float(tot_row[1]) if len(tot_row) > 1 else None
        if line is None:
            line = _as_float(tot_row[0])
        if over and under and line is not None:
            sel_id = str(tot_row[4] if len(tot_row) > 4 else "")
            od = RemoteOdds(
                bet_type="total",
                odds_data={
                    "over": over,
                    "under": under,
                    "_site": {
                        "bet_type": "total",
                        "line": float(line),
                        "mid": mid,
                        "sport_id": str(sport_id),
                        "site_code": "pinnacle",
                        "selections": {
                            "over": {"id": sel_id, "oid": sel_id, "price": over, "name": "over"},
                            "under": {"id": sel_id, "oid": sel_id, "price": under, "name": "under"},
                        },
                    },
                },
                total=float(line),
            )
            out.append(od)

    # 独赢
    ml = p0[2] if isinstance(p0[2], list) else []
    if len(ml) >= 2:
        home = _as_float(ml[0])
        # 篮球常为 [home, away, null, id]；足球 [home, draw, away, id]
        draw = None
        away = None
        if len(ml) >= 3 and ml[2] not in (None, ""):
            # could be draw (soccer) or away if basketball with null draw slot differently
            mid_v = _as_float(ml[1])
            third = _as_float(ml[2])
            if third is not None and mid_v is not None and home:
                # soccer 1X2
                draw = mid_v
                away = third
            elif mid_v is not None and home:
                away = mid_v
        else:
            away = _as_float(ml[1])
        if home and away:
            data: dict[str, Any] = {"home": home, "away": away}
            if draw:
                data["draw"] = draw
            sel_id = str(ml[3] if len(ml) > 3 else "")
            data["_site"] = {
                "bet_type": "moneyline",
                "mid": mid,
                "sport_id": str(sport_id),
                "site_code": "pinnacle",
                "selections": {
                    "home": {"id": sel_id, "oid": sel_id, "price": home, "name": "home"},
                    "away": {"id": sel_id, "oid": sel_id, "price": away, "name": "away"},
                },
            }
            out.append(RemoteOdds(bet_type="moneyline", odds_data=data))
    return out


def parse_compact_events(
    payloads: list[Any],
    *,
    limit: int = 300,
    live_only: bool = True,
) -> list[RemoteMatch]:
    """解析 sports-service/sv/compact/events 响应 → RemoteMatch。"""
    out: list[RemoteMatch] = []
    seen: set[str] = set()
    skipped_virtual = 0

    for body in payloads or []:
        if not isinstance(body, dict):
            continue
        sports = body.get("l")
        if not isinstance(sports, list):
            continue
        for sport_block in sports:
            if not isinstance(sport_block, list) or len(sport_block) < 3:
                continue
            try:
                sport_id = int(sport_block[0])
            except (TypeError, ValueError):
                continue
            sport = SPORT_IDS.get(sport_id)
            if sport not in SUPPORTED_SPORTS:
                continue
            leagues = sport_block[2]
            if not isinstance(leagues, list):
                continue
            for lg in leagues:
                if not isinstance(lg, list) or len(lg) < 3:
                    continue
                league = str(lg[1] or "").strip()
                if not league or league in {
                    "足球滚球", "篮球滚球", "滚球", "足球", "篮球", "体育", "今日",
                    "未知联赛", "未知",
                }:
                    continue
                events = lg[2]
                if not isinstance(events, list):
                    continue
                for ev in events:
                    if not isinstance(ev, list) or len(ev) < 9:
                        continue
                    mid = str(ev[0] or "").strip()
                    home = str(ev[1] or "").strip()
                    away = str(ev[2] or "").strip()
                    if not mid or not home or not away or home == away:
                        continue
                    ext = f"pinnacle:{mid}"
                    if ext in seen:
                        continue

                    if is_virtual_match(sport, league, home, away):
                        skipped_virtual += 1
                        continue
                    from app.services.bookmakers.china_match import is_china_match

                    if is_china_match(league, home, away, sport):
                        skipped_virtual += 1
                        continue

                    running = bool(ev[5]) if len(ev) > 5 else False
                    live_flag = bool(ev[6]) if len(ev) > 6 else False
                    clock = str(ev[15] or "").strip() if len(ev) > 15 else ""
                    period = str(ev[16] or "").strip() if len(ev) > 16 else ""
                    scores = ev[9] if len(ev) > 9 else None
                    hs = aws = 0
                    if isinstance(scores, list) and len(scores) >= 2:
                        try:
                            hs = int(float(scores[0]))
                            aws = int(float(scores[1]))
                        except (TypeError, ValueError):
                            hs = aws = 0

                    is_live = live_flag or running or bool(clock) or bool(period)
                    if live_only and not is_live:
                        # mk=2 接口本身就是滚球；缺标记时仍收（避免漏盘）
                        is_live = True

                    periods = ev[8] if isinstance(ev[8], dict) else {}
                    p0 = periods.get("0") or periods.get(0)
                    odds_list = _odds_from_period0(
                        p0 if isinstance(p0, list) else [],
                        mid=mid,
                        sport_id=sport_id,
                    )
                    if not odds_list:
                        continue

                    seen.add(ext)
                    start = _ms_to_iso(ev[4] if len(ev) > 4 else 0)
                    status = "live" if (live_only or is_live) else "upcoming"
                    out.append(
                        RemoteMatch(
                            external_id=ext,
                            sport=sport,
                            league=league[:100],
                            home_team=home[:100],
                            away_team=away[:100],
                            start_time=start,
                            status=status,
                            venue="Pinnacle",
                            odds_list=odds_list,
                            home_score=hs,
                            away_score=aws,
                            clock=clock[:32] if status == "live" else "",
                            period=period[:64] if status == "live" else "",
                        )
                    )
                    if len(out) >= limit:
                        break
                if len(out) >= limit:
                    break
            if len(out) >= limit:
                break
        if len(out) >= limit:
            break

    if skipped_virtual:
        logger.info("pinnacle compact: skipped %s virtual rows", skipped_virtual)

    for m in out:
        cleaned: list[RemoteOdds] = []
        for o in m.odds_list or []:
            data = normalize_odds_data_to_european(o.odds_data)
            if any(not str(k).startswith("_") for k in data):
                cleaned.append(
                    RemoteOdds(
                        bet_type=o.bet_type,
                        odds_data=data,
                        spread=o.spread,
                        total=o.total,
                    )
                )
        m.odds_list = cleaned
    return [m for m in out if m.odds_list]


async def _collect_origins(page: Any, base_url: str = "") -> list[str]:
    origins: list[str] = []
    seen: set[str] = set()

    def add(raw: str) -> None:
        u = (raw or "").strip()
        if not u or "://" not in u:
            return
        try:
            p = urlparse(u)
            if not p.netloc:
                return
            o = f"{p.scheme}://{p.netloc}"
            if o not in seen:
                seen.add(o)
                origins.append(o)
        except Exception:
            return

    add(base_url)
    try:
        add(page.url or "")
    except Exception:
        pass
    try:
        for fr in page.frames:
            try:
                add(fr.url or "")
            except Exception:
                continue
    except Exception:
        pass
    return origins


def _compact_events_url(origin: str, *, sport_id: int, live_only: bool) -> str:
    mk = 2 if live_only else 3
    l_filter = 2 if live_only else 0
    qs = (
        f"sp={sport_id}&lg=&ev=&mk={mk}&btg=1&ot=1&d=&o=0&l={l_filter}"
        f"&v=&lv=&me=0&more=false&tm=0&pa=0&c=Others&pn=-1&cl=-1"
        f"&hle=true&inl=false&pv=1&ic=false&ice=false&withCredentials=true&lang=zh_CN"
    )
    return f"{origin.rstrip('/')}/sports-service/sv/compact/events?{qs}"


async def _capture_compact_from_network(page: Any, *, wait_ms: int = 2800) -> list[Any]:
    """短听 XHR：复用 SPA 自己打的 compact/events（带正确 g/v 版本参数）。"""
    captured: list[Any] = []

    async def on_response(resp) -> None:
        try:
            url = (resp.url or "").lower()
            if "sports-service/sv/compact/events" not in url:
                return
            if resp.status != 200:
                return
            body = await resp.json()
            if isinstance(body, dict) and isinstance(body.get("l"), list):
                captured.append(body)
        except Exception:
            return

    try:
        page.on("response", on_response)
    except Exception:
        return []
    try:
        # 轻触滚球盘，促发 SPA 刷新盘口 XHR（evaluate，避免 locator 超时）
        try:
            await page.evaluate(
                """() => {
                  const nodes = Array.from(document.querySelectorAll('a,button,span,div'));
                  const hit = nodes.find((e) => {
                    const t = (e.innerText || '').replace(/\\s+/g, ' ').trim();
                    return t === '滚球盘' || t === 'Live' || t === 'In-Play';
                  });
                  if (hit) hit.click();
                }"""
            )
        except Exception:
            pass
        await page.wait_for_timeout(max(800, int(wait_ms)))
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass
    return captured


async def _fetch_via_guest_http(origins: list[str], *, live_only: bool) -> list[Any]:
    """无 cookie 访客拉取（登录态带 cookie 时 sports-service 常 400）。"""
    payloads: list[Any] = []
    try:
        import httpx
    except Exception:
        return payloads
    timeout = httpx.Timeout(20.0, connect=8.0)
    async with httpx.AsyncClient(verify=False, timeout=timeout, follow_redirects=True) as client:
        for origin in origins[:3]:
            for sp in SPORT_IDS:
                url = _compact_events_url(origin, sport_id=sp, live_only=live_only)
                try:
                    resp = await client.get(
                        url,
                        headers={
                            "accept": "application/json, text/plain, */*",
                            "user-agent": (
                                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) "
                                "Chrome/120.0.0.0 Safari/537.36"
                            ),
                            "referer": f"{origin.rstrip('/')}/zh-cn/compact/sports/soccer/live",
                        },
                    )
                    if resp.status_code != 200:
                        logger.info(
                            "pinnacle compact guest fail status=%s sp=%s",
                            resp.status_code,
                            sp,
                        )
                        continue
                    body = resp.json()
                    if isinstance(body, dict) and isinstance(body.get("l"), list):
                        payloads.append(body)
                except Exception as e:
                    logger.info("pinnacle compact guest err sp=%s: %s", sp, e)
            if len(payloads) >= len(SPORT_IDS):
                break
    return payloads


async def fetch_pinnacle_live_odds(
    page: Any,
    *,
    base_url: str = "",
    limit: int = 80,
    live_only: bool = True,
) -> list[RemoteMatch]:
    """页内 / Playwright request 拉取 compact/events（足球+篮球）。"""
    if page is None:
        return []
    try:
        if page.is_closed():
            return []
    except Exception:
        return []

    origins = await _collect_origins(page, base_url=base_url)
    sport_ids = list(SPORT_IDS.keys())
    payloads: list[Any] = []
    try:
        result = await page.evaluate(
            _FETCH_JS,
            {
                "liveOnly": bool(live_only),
                "sportIds": sport_ids,
                "origins": origins,
            },
        )
        if isinstance(result, dict):
            payloads = list(result.get("payloads") or [])
            logger.info(
                "pinnacle compact fetch: n=%s origins=%s debug=%s",
                result.get("n"),
                result.get("origins"),
                result.get("debug"),
            )
        elif isinstance(result, list):
            payloads = result
    except Exception as e:
        logger.warning("pinnacle compact fetch evaluate failed: %s", e)

    if not payloads:
        payloads = await _capture_compact_from_network(page, wait_ms=2800)
        logger.info("pinnacle compact via xhr capture: n=%s", len(payloads))

    if not payloads:
        payloads = await _fetch_via_guest_http(origins, live_only=bool(live_only))
        logger.info("pinnacle compact via guest http: n=%s", len(payloads))

    rows = parse_compact_events(payloads, limit=limit, live_only=bool(live_only))
    sports = {m.sport for m in rows}
    logger.info(
        "pinnacle compact parsed=%d sports=%s live_only=%s",
        len(rows),
        sorted(sports),
        live_only,
    )
    return rows
