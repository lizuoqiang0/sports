"""
多站点盘口嗅探：足球/篮球 · 独赢/让球/大小 · 写入 _site 下单引用。
与 OB 的 parse_matches_pb 字段能力对齐（市场类型 + 投注引用）。
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from app.services.bookmakers.base import RemoteMatch, RemoteOdds
from app.services.bookmakers.plugins.ob.odds import is_virtual_match
from app.services.bookmakers.site_profiles import get_site_profile
from app.services.bookmakers.sport_classify import (
    classify_sport,
    reject_sport_mismatch,
)
from app.services.odds_domain import coerce_float_european, normalize_odds_data_to_european

logger = logging.getLogger(__name__)

_SUPPORTED = {"football", "basketball"}


def _as_float(v: Any) -> Optional[float]:
    """统一解析为亚洲盘小数赔率。"""
    return coerce_float_european(v)


def _as_line(v: Any) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _guess_sport(text: str) -> str:
    """兼容旧调用；严格分类见 classify_sport。"""
    return classify_sport(text=text) or "other"


def _walk(obj: Any, depth: int = 0):
    if depth > 8:
        return
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v, depth + 1)
    elif isinstance(obj, list):
        for item in obj[:500]:
            yield from _walk(item, depth + 1)


def _pick_str(d: dict, *keys: str) -> str:
    for k in keys:
        v = d.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if s and s.lower() not in ("null", "none", "undefined"):
            return s
    return ""


def _extract_teams(d: dict) -> tuple[str, str]:
    home = _pick_str(
        d,
        "home",
        "homeName",
        "home_name",
        "homeTeam",
        "home_team",
        "team1",
        "team1Name",
        "homeTeamName",
        "HomeTeam",
        "team_h",
        "homeTeamCn",
        "homeParticipant",
        "participant1",
        "hteam",
        "home_team_name",
    )
    away = _pick_str(
        d,
        "away",
        "awayName",
        "away_name",
        "awayTeam",
        "away_team",
        "team2",
        "team2Name",
        "awayTeamName",
        "AwayTeam",
        "team_a",
        "team_c",
        "awayTeamCn",
        "awayParticipant",
        "participant2",
        "ateam",
        "away_team_name",
    )
    if not home or not away:
        for hk, ak in (
            ("homeTeam", "awayTeam"),
            ("home", "away"),
            ("team1", "team2"),
            ("homeParticipant", "awayParticipant"),
            ("participant1", "participant2"),
        ):
            h, a = d.get(hk), d.get(ak)
            if isinstance(h, dict) and isinstance(a, dict):
                home = home or _pick_str(
                    h, "name", "teamName", "cnName", "name_zh", "text", "nameCn", "n", "label"
                )
                away = away or _pick_str(
                    a, "name", "teamName", "cnName", "name_zh", "text", "nameCn", "n", "label"
                )
    if (not home or not away) and isinstance(d.get("participants"), list):
        names = []
        for p in d.get("participants") or []:
            if isinstance(p, dict):
                nm = _pick_str(p, "name", "teamName", "cnName", "name_zh", "text", "nameCn", "n")
                if nm:
                    names.append(nm)
            elif isinstance(p, str) and p.strip():
                names.append(p.strip())
        if len(names) >= 2:
            home, away = names[0], names[1]
    if (not home or not away) and isinstance(d.get("teams"), list):
        names = [str(x).strip() for x in (d.get("teams") or []) if str(x).strip()]
        if len(names) >= 2:
            home, away = names[0], names[1]
    return home[:100], away[:100]


def _sel_key_from_name(name: str) -> Optional[str]:
    n = (name or "").lower()
    if any(x in n for x in ("home", "1", "主", "主队", "host")):
        return "home"
    if any(x in n for x in ("away", "2", "客", "客队", "guest")):
        return "away"
    if any(x in n for x in ("draw", "x", "平", "和")):
        return "draw"
    if any(x in n for x in ("over", "大", "o ")):
        return "over"
    if any(x in n for x in ("under", "小", "u ")):
        return "under"
    return None


def _selection_ref(item: dict, price: float) -> dict:
    return {
        "id": _pick_str(item, "id", "oid", "selectionId", "selId", "oddsId", "outcomeId", "hid", "key"),
        "hid": _pick_str(item, "hid", "handicapId", "marketId", "lineId"),
        "oid": _pick_str(item, "oid", "oddsId", "selectionId", "id"),
        "price": price,
        "name": _pick_str(item, "name", "label", "selection", "outcome", "type", "key"),
    }


def _extract_moneyline(d: dict) -> Optional[RemoteOdds]:
    home = _as_float(d.get("home") or d.get("homeOdds") or d.get("oddsH") or d.get("h") or d.get("odds1"))
    away = _as_float(d.get("away") or d.get("awayOdds") or d.get("oddsA") or d.get("a") or d.get("odds2"))
    draw = _as_float(d.get("draw") or d.get("drawOdds") or d.get("oddsD") or d.get("d") or d.get("oddsX"))
    sels: dict[str, dict] = {}
    if home and away:
        data = {"home": home, "away": away}
        if draw:
            data["draw"] = draw
        return RemoteOdds(bet_type="moneyline", odds_data=data)

    prices = d.get("prices") or d.get("odds") or d.get("selections") or d.get("runners") or d.get("outcomes")
    if isinstance(prices, list) and len(prices) >= 2:
        data: dict[str, float] = {}
        for p in prices:
            if not isinstance(p, dict):
                continue
            name = _pick_str(p, "name", "label", "selection", "outcome", "type", "key", "title")
            key = _sel_key_from_name(name) or _sel_key_from_name(str(p.get("type") or p.get("side") or ""))
            price = _as_float(p.get("price") or p.get("odds") or p.get("odd") or p.get("value") or p.get("decimal"))
            if not key or not price or key not in ("home", "away", "draw"):
                continue
            data[key] = price
            sels[key] = _selection_ref(p, price)
        if "home" in data and "away" in data:
            od = RemoteOdds(bet_type="moneyline", odds_data=data)
            if sels:
                od.odds_data["_site"] = {"bet_type": "moneyline", "selections": sels}
            return od

    for mk in ("ml", "moneyline", "1x2", "matchOdds", "markets", "market", "had", "win"):
        sub = d.get(mk)
        if isinstance(sub, dict):
            od = _extract_moneyline(sub)
            if od:
                return od
        if isinstance(sub, list):
            for item in sub:
                if isinstance(item, dict):
                    # market type filter
                    mtype = _pick_str(item, "type", "marketType", "name", "key", "betType").lower()
                    if mtype and not any(x in mtype for x in ("1x2", "ml", "money", "独赢", "胜平负", "win", "had", "")):
                        if any(x in mtype for x in ("spread", "handicap", "让", "ah", "total", "ou", "大小")):
                            continue
                    od = _extract_moneyline(item)
                    if od:
                        return od
    return None


def _extract_spread(d: dict) -> Optional[RemoteOdds]:
    line = _as_line(
        d.get("spread")
        or d.get("handicap")
        or d.get("hdp")
        or d.get("line")
        or d.get("handicapValue")
        or d.get("points")
    )
    home = _as_float(d.get("home") or d.get("homeOdds") or d.get("oddsH"))
    away = _as_float(d.get("away") or d.get("awayOdds") or d.get("oddsA"))
    sels: dict[str, dict] = {}

    # nested market list
    markets = d.get("markets") or d.get("market") or d.get("handicap") or d.get("ah") or d.get("spread")
    candidates = []
    if isinstance(markets, list):
        candidates = markets
    elif isinstance(markets, dict):
        candidates = [markets]
    for item in candidates:
        if not isinstance(item, dict):
            continue
        mtype = _pick_str(item, "type", "marketType", "name", "key", "betType").lower()
        if mtype and not any(x in mtype for x in ("spread", "handicap", "让", "ah", "hdp", "asian")):
            if any(x in mtype for x in ("1x2", "money", "独赢", "total", "ou", "大小")):
                continue
        ln = _as_line(item.get("line") or item.get("handicap") or item.get("hdp") or item.get("spread") or line)
        prices = item.get("prices") or item.get("odds") or item.get("selections") or item.get("outcomes")
        data: dict[str, float] = {}
        local_sels: dict[str, dict] = {}
        if isinstance(prices, list):
            for p in prices:
                if not isinstance(p, dict):
                    continue
                key = _sel_key_from_name(_pick_str(p, "name", "label", "selection", "side", "type"))
                price = _as_float(p.get("price") or p.get("odds") or p.get("odd") or p.get("value"))
                if key in ("home", "away") and price:
                    data[key] = price
                    local_sels[key] = _selection_ref(p, price)
        else:
            h = _as_float(item.get("home") or item.get("oddsH"))
            a = _as_float(item.get("away") or item.get("oddsA"))
            if h and a:
                data = {"home": h, "away": a}
        if "home" in data and "away" in data:
            od = RemoteOdds(bet_type="spread", odds_data=data, spread=float(ln or 0))
            if local_sels:
                od.odds_data["_site"] = {
                    "bet_type": "spread",
                    "line": float(ln or 0),
                    "selections": local_sels,
                }
            return od

    if home and away and line is not None:
        return RemoteOdds(
            bet_type="spread",
            odds_data={"home": home, "away": away},
            spread=float(line),
        )
    return None


def _extract_total(d: dict) -> Optional[RemoteOdds]:
    line = _as_line(d.get("total") or d.get("points") or d.get("line") or d.get("ou") or d.get("goalLine"))
    over = _as_float(d.get("over") or d.get("oddsO") or d.get("o"))
    under = _as_float(d.get("under") or d.get("oddsU") or d.get("u"))

    markets = d.get("markets") or d.get("market") or d.get("total") or d.get("ou") or d.get("overUnder")
    candidates = []
    if isinstance(markets, list):
        candidates = markets
    elif isinstance(markets, dict):
        candidates = [markets]
    for item in candidates:
        if not isinstance(item, dict):
            continue
        mtype = _pick_str(item, "type", "marketType", "name", "key", "betType").lower()
        if mtype and not any(x in mtype for x in ("total", "ou", "over", "under", "大小", "大/小")):
            if any(x in mtype for x in ("1x2", "money", "独赢", "spread", "handicap", "让")):
                continue
        ln = _as_line(item.get("line") or item.get("total") or item.get("points") or line)
        prices = item.get("prices") or item.get("odds") or item.get("selections") or item.get("outcomes")
        data: dict[str, float] = {}
        local_sels: dict[str, dict] = {}
        if isinstance(prices, list):
            for p in prices:
                if not isinstance(p, dict):
                    continue
                key = _sel_key_from_name(_pick_str(p, "name", "label", "selection", "side", "type"))
                price = _as_float(p.get("price") or p.get("odds") or p.get("odd") or p.get("value"))
                if key in ("over", "under") and price:
                    data[key] = price
                    local_sels[key] = _selection_ref(p, price)
        else:
            o = _as_float(item.get("over") or item.get("oddsO"))
            u = _as_float(item.get("under") or item.get("oddsU"))
            if o and u:
                data = {"over": o, "under": u}
        if "over" in data and "under" in data:
            od = RemoteOdds(bet_type="total", odds_data=data, total=float(ln or 0))
            if local_sels:
                od.odds_data["_site"] = {
                    "bet_type": "total",
                    "line": float(ln or 0),
                    "selections": local_sels,
                }
            return od

    if over and under and line is not None:
        return RemoteOdds(
            bet_type="total",
            odds_data={"over": over, "under": under},
            total=float(line),
        )
    return None


def _extract_score(d: dict) -> tuple[int, int, str, str]:
    hs = d.get("homeScore") or d.get("home_score") or d.get("scoreH") or d.get("hs") or d.get("homeGoals")
    as_ = d.get("awayScore") or d.get("away_score") or d.get("scoreA") or d.get("as") or d.get("awayGoals")
    try:
        home_score = int(float(hs)) if hs is not None and str(hs) != "" else 0
        away_score = int(float(as_)) if as_ is not None and str(as_) != "" else 0
    except (TypeError, ValueError):
        home_score = away_score = 0
        score = str(d.get("score") or d.get("result") or d.get("currentScore") or "")
        m = re.match(r"(\d+)\s*[-:]\s*(\d+)", score)
        if m:
            home_score, away_score = int(m.group(1)), int(m.group(2))
    clock = _pick_str(d, "clock", "timer", "time", "matchTime", "liveTime", "playedTime", "minute")
    if clock.isdigit():
        clock = f"{clock}:00"
    period = _pick_str(d, "period", "periodName", "statusText", "phase", "quarter", "section", "half")
    return home_score, away_score, clock[:32], period[:64]


def _attach_match_meta(odds_list: list[RemoteOdds], *, mid: str, site_code: str, raw: dict) -> None:
    meta_extra = {
        "mid": mid,
        "site_code": site_code,
        "league_id": _pick_str(raw, "leagueId", "league_id", "competitionId", "tournamentId"),
        "sport_id": _pick_str(raw, "sportId", "sport_id", "csid"),
    }
    for od in odds_list:
        site = dict(od.odds_data.get("_site") or {})
        site.update(meta_extra)
        od.odds_data["_site"] = site


def parse_captured_payloads(
    payloads: list[Any],
    *,
    site_code: str,
    limit: int = 300,
    live_only: bool = False,
) -> list[RemoteMatch]:
    out: list[RemoteMatch] = []
    seen: set[str] = set()
    prefix = (site_code or "site").lower()
    drop = {
        "no_teams": 0,
        "no_odds": 0,
        "no_league": 0,
        "sport": 0,
        "mismatch": 0,
        "virtual": 0,
        "not_live": 0,
        "dup": 0,
    }
    live_flagged = 0

    for body in payloads:
        for d in _walk(body):
            if not isinstance(d, dict):
                continue
            home, away = _extract_teams(d)
            if not home or not away or home == away:
                drop["no_teams"] += 1
                continue

            ml = _extract_moneyline(d)
            sp = _extract_spread(d)
            tot = _extract_total(d)
            odds_list = [x for x in (ml, sp, tot) if x]
            if not odds_list:
                drop["no_odds"] += 1
                continue

            league = _pick_str(
                d,
                "league",
                "leagueName",
                "league_name",
                "competition",
                "competitionName",
                "tournament",
                "tournamentName",
                "lname",
                "leagueNameCn",
            ) or ""
            league = league.strip()
            hs, aws, clock, period = _extract_score(d)
            sport = classify_sport(
                sport_id=_pick_str(d, "sportId", "sport_id", "csid", "sid", "ballType"),
                sport_field=_pick_str(d, "sport", "sportName", "sport_name", "ball", "gameType"),
                text=" ".join(
                    [
                        league,
                        home,
                        away,
                        _pick_str(d, "sport", "sportName", "sport_name", "ball", "gameType"),
                    ]
                ),
                period=period,
                home_score=hs,
                away_score=aws,
            )
            # 无真实联赛名一律丢弃（禁止「足球滚球/篮球滚球」占位脏数据）
            _ph_lg = {
                "", "未知联赛", "未知", "N/A", "-", "—",
                "足球滚球", "篮球滚球", "滚球", "足球", "篮球", "体育", "今日",
            }
            if (not league) or league in _ph_lg:
                drop["no_league"] += 1
                continue
            if sport not in _SUPPORTED:
                drop["sport"] += 1
                continue
            if reject_sport_mismatch(
                sport,
                period=period,
                home_score=hs,
                away_score=aws,
                text=f"{league} {home} {away}",
            ):
                drop["mismatch"] += 1
                continue
            if is_virtual_match(sport, league, home, away):
                drop["virtual"] += 1
                continue
            from app.services.bookmakers.china_match import is_china_match

            if is_china_match(league, home, away, sport):
                drop["china"] = drop.get("china", 0) + 1
                continue

            mid = _pick_str(
                d,
                "id",
                "matchId",
                "match_id",
                "eventId",
                "event_id",
                "fixtureId",
                "gameId",
                "eid",
                "eventIdStr",
            )
            if not mid:
                mid = f"{sport}|{league}|{home}|{away}"
            ext = f"{prefix}:{mid}"
            if ext in seen:
                drop["dup"] += 1
                continue
            seen.add(ext)

            _attach_match_meta(odds_list, mid=str(mid), site_code=prefix, raw=d)

            home_score, away_score = hs, aws
            status_raw = _pick_str(
                d, "status", "matchStatus", "state", "liveStatus", "rb", "eventStatus", "es"
            ).lower()
            # 严格滚球：仅显式 live 标记或「进行中节次」；禁止 clock/period 单独标 live（会误吃今日/早盘）
            from app.services.bookmakers.match_live import has_inplay_period, is_actually_started

            live_raw = d.get("isLive")
            if live_raw is None:
                live_raw = d.get("is_live")
            if live_raw is None:
                live_raw = d.get("live")
            if live_raw is None:
                live_raw = d.get("inPlay")
            if live_raw is None:
                live_raw = d.get("inplay")
            if live_raw is None:
                live_raw = d.get("is_inplay")
            flag_live = False
            if isinstance(live_raw, bool):
                flag_live = live_raw
            elif isinstance(live_raw, (int, float)):
                flag_live = int(live_raw) == 1
            else:
                flag_live = str(live_raw or "").lower() in ("1", "true", "yes", "live", "inplay")
            # 平博常见：status 数值 1/2=滚球；仅当已有比分或时钟时采纳
            if not flag_live and str(d.get("status") or "").strip() in ("1", "2"):
                if (hs or aws) or clock:
                    flag_live = True
            status_live = any(
                x in status_raw for x in ("live", "inplay", "running", "进行", "滚球", "in_play", "started")
            )
            is_live = flag_live or status_live or has_inplay_period(period)
            if is_live:
                live_flagged += 1
            status = "live" if is_live else "upcoming"
            start = _pick_str(
                d, "startTime", "start_time", "kickoff", "beginTime", "eventDate", "date", "matchTime", "openTime"
            )

            if status == "live" and not is_actually_started(
                status=status,
                period=period,
                clock=clock,
                home_score=home_score,
                away_score=away_score,
                start_time=start,
            ):
                status = "upcoming"
            # 产品范围：只采滚球足球/篮球（live_only 默认 True；非 live 一律丢弃）
            if live_only or True:
                if status != "live":
                    drop["not_live"] += 1
                    continue
            odds_list = [
                RemoteOdds(
                    bet_type=o.bet_type,
                    odds_data=normalize_odds_data_to_european(o.odds_data),
                    spread=o.spread,
                    total=o.total,
                )
                for o in (odds_list or [])
                if o and normalize_odds_data_to_european(o.odds_data)
            ]
            if not odds_list:
                drop["no_odds"] += 1
                continue
            out.append(
                RemoteMatch(
                    external_id=ext,
                    sport=sport,
                    league=league[:100],
                    home_team=home,
                    away_team=away,
                    start_time=start,
                    status=status,
                    venue=get_site_profile(site_code).get("name") or site_code,
                    odds_list=odds_list,
                    home_score=home_score,
                    away_score=away_score,
                    clock=clock if status == "live" else "",
                    period=period if status == "live" else "",
                )
            )
            if len(out) >= limit:
                logger.info(
                    "xhr parse %s: out=%d live_flagged=%d drop=%s (hit limit)",
                    prefix,
                    len(out),
                    live_flagged,
                    drop,
                )
                return out
    if payloads:
        logger.info(
            "xhr parse %s: out=%d live_flagged=%d drop=%s payloads=%d",
            prefix,
            len(out),
            live_flagged,
            drop,
            len(payloads),
        )
        # 平博采空时抽样字段名，便于补齐 team/live 映射
        if prefix == "pinnacle" and not out and drop.get("no_teams", 0) > 50:
            samples = []
            for body in payloads[:20]:
                for d in _walk(body):
                    if not isinstance(d, dict):
                        continue
                    keys = list(d.keys())
                    keyset = {str(k).lower() for k in keys}
                    if not keyset.intersection(
                        {"price", "odds", "prices", "home", "away", "islive", "inplay", "participants", "teams"}
                    ):
                        continue
                    samples.append(sorted(keys)[:24])
                    if len(samples) >= 3:
                        break
                if len(samples) >= 3:
                    break
            if samples:
                logger.info("xhr parse pinnacle key samples: %s", samples)
    return out


async def fetch_site_odds_via_page(
    page,
    *,
    site_code: str,
    base_url: str,
    limit: int = 300,
    live_only: bool = False,
    wait_ms: int = 5000,
) -> list[RemoteMatch]:
    """
    多站点盘口。登录阶段已要求手动进场馆；此处不再乱跳 sports_paths。
    """
    from app.services.bookmakers.site_profiles import needs_manual_venue
    from app.services.bookmakers.venue_live import fetch_venue_live_odds

    code = (site_code or "").lower()
    # 滚球默认更短等待；全量仍可用较大 wait_ms
    eff_wait = wait_ms
    if live_only and wait_ms > 3000:
        eff_wait = 2200
    # 平博必须走 venue_live（会 goto /soccer/live|/basketball/live）；勿停在早盘 sports 列表
    if (
        needs_manual_venue(site_code)
        or get_site_profile(site_code).get("portal")
        or code == "pinnacle"
    ):
        return await fetch_venue_live_odds(
            page,
            site_code=site_code,
            base_url=base_url,
            limit=limit,
            live_only=live_only,
            wait_ms=eff_wait,
        )

    from app.services.bookmakers.venue_entry import activate_sportsbook_tabs, is_in_sportsbook

    hints = tuple(get_site_profile(site_code).get("odds_url_hints") or ())
    captured: list[Any] = []

    async def on_response(resp):
        try:
            url = (resp.url or "").lower()
            if hints and not any(h.lower() in url for h in hints):
                if not any(x in url for x in ("sport", "odds", "match", "event", "fixture", "league", "market")):
                    return
            ctype = (resp.headers.get("content-type") or "").lower()
            if "json" not in ctype and "javascript" not in ctype and "text" not in ctype:
                return
            body = await resp.json()
            if body is not None:
                captured.append(body)
        except Exception:
            return

    page.on("response", on_response)
    try:
        from app.services.bookmakers.venue_entry import page_already_on_live_board

        in_book = await is_in_sportsbook(page)
        on_live = False
        try:
            on_live = await page_already_on_live_board(page)
        except Exception:
            on_live = False
        if not in_book:
            logger.warning("site odds %s: not in sportsbook, trying DOM scrape anyway", site_code)
        # 已在滚球盘：不点 Tab、轻滚即可
        if not on_live:
            try:
                await activate_sportsbook_tabs(page, live_only=live_only, gentle=True)
            except Exception:
                pass
        await page.wait_for_timeout(max(1500, wait_ms // 2) if on_live else max(2000, wait_ms))
        try:
            await page.mouse.wheel(0, 800 if on_live else 2800)
            await page.wait_for_timeout(600 if on_live else 1200)
        except Exception:
            pass
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass

    parsed = parse_captured_payloads(
        captured,
        site_code=site_code,
        limit=limit,
        live_only=live_only,
    )
    if len(parsed) < 5:
        from app.services.bookmakers.venue_live import scrape_dom_matches

        for m in await scrape_dom_matches(page, site_code=site_code, live_only=live_only, limit=limit):
            if all(m.external_id != x.external_id for x in parsed):
                parsed.append(m)
            if len(parsed) >= limit:
                break

    logger.info(
        "site odds %s: payloads=%d parsed=%d live_only=%s",
        site_code,
        len(captured),
        len(parsed),
        live_only,
    )
    return parsed[:limit]
