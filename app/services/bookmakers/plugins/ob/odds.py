"""
开云体育（YBTY）真实盘口拉取。

流程：注入站点 X-API-TOKEN → /game/sport → 启动 YBTY → 打开 H5
→ yewu11/v1/m/matchesPB (hpsFlag=1) → 解析全场独赢/让球/大小。
"""
from __future__ import annotations

import base64
import gzip
import json
import logging
import re
import time
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from app.services.bookmakers.base import RemoteMatch, RemoteOdds

logger = logging.getLogger(__name__)

# 仅保留：足球、篮球
SUPPORTED_SPORTS = {"football", "basketball"}

# 虚拟赛事关键词 —— 足球/篮球虚拟盘（EAFC、NBA2K、VS-、PANDA、手柄图标短时杯）一律不采集
_VIRTUAL_KEYWORDS = (
    "虚拟", "VR", "Virtual", "模拟", "仿真",
    "电子足球", "电子篮球", "电子体育", "电子联赛",
    "数字联赛", "数字足球", "数字篮球",
    "EAFC", "EA FC", "EA Football", "EA Sports FC", "EASPORTS FC",
    "FIFA24", "FIFA25", "FIFA26", "FIFA23", "FIFA22",
    "FIFA 24", "FIFA 25", "FIFA 26", "FIFA 23", "FIFA 22",
    "FC24", "FC25", "FC26", "FC 24", "FC 25", "FC 26",
    "eFootball", "PES ", "Cyber Football",
    "虚拟联赛", "模拟联赛",
    "E-Soccer", "eSoccer", "Esoccer", "ESoccer",
    "PANDA独家", "PANDA 独家",
    "NBA 2K", "NBA2K", "2K23", "2K24", "2K25", "2K26",
    # 站点「手柄图标」短时电子足球/篮球杯（如瓦尔基里杯 / 瓦尔哈拉杯）
    "瓦尔基里", "瓦尔哈拉", "Valkyrie", "Valhalla",
    "手柄", "gamepad", "Game Pad",
)

# 强规则：命中即虚拟 —— 覆盖 VS-/EAFC/NBA2K/PANDA/手柄短时杯
_VIRTUAL_REGEX = re.compile(
    r"("
    r"eafc\s*\d*"
    r"|ea\s*fc\s*\d*"
    r"|ea\s*sports?\s*fc"
    r"|fifa\s*\d{2}"
    r"|fc\s*2[2-9]"
    r"|e-?soccer"
    r"|efootball"
    r"|电子足球"
    r"|电子篮球"
    r"|虚拟\s*(足球|篮球|联赛|赛事|比赛)?"
    r"|模拟\s*(足球|篮球|联赛|赛事)?"
    r"|panda\s*独家"
    r"|nba\s*2k"
    r"|2k\s*2[3-9]"
    r"|vs\s*[-–—]"  # VS- 前缀（OB 虚拟足球/篮球）
    r"|瓦尔基里|瓦尔哈拉|valkyrie|valhalla"
    r"|手柄|game\s*pad|gamepad"
    r")",
    re.I,
)

# 手柄图标类短时赛：联赛名形如「瓦尔基里杯 2026 (8分钟)」；(2x40分钟) 等真实赛制不命中
_SHORT_CUP_MINUTES_RE = re.compile(r"\(\s*\d{1,2}\s*分钟\s*\)")


def is_virtual_match(sport: str, league: str, home: str = "", away: str = "", mcls: str = "") -> bool:
    """严格检测虚拟赛事：足球 EAFC / 篮球 NBA2K / VS- / PANDA / 手柄短时杯等一律排除。

    判定标准（命中任一即虚拟，禁止入库与展示）：
    1. 联赛/队名命中 EAFC、FIFA、FC24+、NBA2K、VS-、PANDA独家、瓦尔基里/瓦尔哈拉
    2. 联赛/队名包含虚拟关键词或手柄图标相关文案
    3. 足球/篮球且 mcls 标记为虚拟（mcls=2/3）
    4. 足球/篮球联赛名以 VS 开头（虚拟体育盘）
    5. 足球/篮球联赛名含短时分钟标记，如 (8分钟)/(10分钟)（站点手柄图标赛）
    """
    text = f"{league or ''} {home or ''} {away or ''}"
    if _VIRTUAL_REGEX.search(text):
        return True
    low = text.lower()
    if any(kw.lower() in low for kw in _VIRTUAL_KEYWORDS):
        return True
    sport_l = (sport or "").lower()
    # 足球/篮球：VS- 虚拟盘、mcls 虚拟分类、手柄短时杯
    if sport_l in ("football", "basketball", "soccer"):
        if re.match(r"^\s*vs\b", (league or ""), flags=re.I):
            return True
        if str(mcls) in ("2", "3"):
            return True
        if _SHORT_CUP_MINUTES_RE.search(league or ""):
            return True
    elif str(mcls) in ("2", "3") and sport_l in ("", "other"):
        return True
    return False


# 兼容旧名
_is_virtual_match = is_virtual_match

SPORT_MAP = {
    "1": "football",
    "2": "basketball",
    "足球": "football",
    "篮球": "basketball",
}

# 开云体育 mmp（比赛阶段）常见映射
# 足球 / 篮球共用一套码表；篮球节次常见 13–16、61–64
_PERIOD_LABELS = {
    "1": "未开赛",
    "2": "上半场",
    "3": "中场",
    "4": "下半场",
    "5": "加时",
    "6": "上半场",
    "7": "下半场",
    "8": "完场",
    "9": "推迟",
    "10": "中断",
    "11": "待定",
    "12": "取消",
    "13": "第1节",  # 篮球
    "14": "第2节",  # 篮球
    "15": "第3节",  # 篮球
    "16": "第4节",  # 篮球
    "17": "节间休息",
    "18": "加时",
    "21": "上半场",  # 篮球上下半场制
    "22": "下半场",
    "31": "中场",
    "32": "等待加时",
    "33": "等待点球",
    "34": "点球大战",
    "40": "加时",
    "41": "加时上半",
    "42": "加时下半",
    "50": "点球",
    "61": "第1节",
    "62": "第2节",
    "63": "第3节",
    "64": "第4节",
    "80": "加时",
    "100": "完场",
    "999": "完场",
}


def _safe_int_score(val: Any) -> Optional[int]:
    try:
        return int(float(str(val).strip()))
    except (TypeError, ValueError):
        return None


def _parse_score_pair(text: str) -> tuple[int, int]:
    s = str(text or "").strip().replace("-", ":").replace("：", ":")
    if ":" not in s:
        return 0, 0
    a, b = s.split(":", 1)
    ha, aa = _safe_int_score(a), _safe_int_score(b)
    if ha is None or aa is None:
        return 0, 0
    return ha, aa


def _parse_msc_score(msc: Any) -> tuple[int, int]:
    """从 matchesPB.msc 解析当前比分。优先 S1（全场/当前），其次 S0。"""
    if not isinstance(msc, list):
        return 0, 0
    preferred = ("S1|", "S0|", "S3|", "S2|")
    for pref in preferred:
        for item in msc:
            s = str(item)
            if s.startswith(pref) and "|" in s:
                return _parse_score_pair(s.split("|", 1)[1])
    # 兜底：仅接受「S+单个数字」键（S1/S0/S2/S3...），拒绝 S13/S14 等
    # 多位段键 —— 那些是篮球单节比分，误当全场分会严重污染结算输入。
    for item in msc:
        s = str(item)
        if "|" not in s:
            continue
        key, part = s.split("|", 1)
        if len(key) == 2 and key[0] == "S" and key[1].isdigit():
            if ":" in part or "-" in part:
                hs, aws = _parse_score_pair(part)
                if hs or aws:
                    return hs, aws
    return 0, 0


def _fmt_seconds_clock(sec: int) -> str:
    if sec < 0:
        sec = 0
    if sec > 24 * 3600:
        return ""
    return f"{sec // 60}:{sec % 60:02d}"


def _parse_ob_clock(row: dict) -> str:
    """解析滚球进行时间。优先可读字段，其次秒数 mst / mststr（兼容毫秒）。"""
    for key in ("mat", "mststs", "mess"):
        v = row.get(key)
        if isinstance(v, str) and ":" in v and len(v.strip()) <= 12:
            return v.strip()
    for key in ("mst", "mststr", "mes"):
        v = row.get(key)
        if v is None or v == "":
            continue
        s = str(v).strip()
        if s.isdigit():
            n = int(s)
            # 开云偶发毫秒：> 1 天秒数则按毫秒降级
            if n > 24 * 3600:
                n = n // 1000
            # 篮球单节通常 < 12 分钟；足球半场 < 60+补时
            if n > 3 * 3600:
                continue
            clock = _fmt_seconds_clock(n)
            if clock:
                return clock
        elif ":" in s and len(s) <= 12:
            return s
    return ""


def _parse_ob_period(row: dict, sport: str = "") -> str:
    mmp = str(row.get("mmp") or "").strip()
    if mmp in _PERIOD_LABELS:
        return _PERIOD_LABELS[mmp]
    # 部分接口用 mlet / mststi 文案
    for key in ("mlet", "mststi", "mfo"):
        v = str(row.get(key) or "").strip()
        if v and len(v) <= 20 and not v.isdigit():
            # 去掉「阶段」前缀噪声
            if v.startswith("阶段") and v[2:].isdigit():
                code = v[2:]
                if code in _PERIOD_LABELS:
                    return _PERIOD_LABELS[code]
            return v
    if mmp and mmp not in ("0", "None"):
        # 篮球未收录码：尽量别直接甩「阶段N」
        if (sport or "").lower() in ("basketball", "2") and mmp.isdigit():
            n = int(mmp)
            if 1 <= n <= 4:
                return f"第{n}节"
        return f"阶段{mmp}"
    return ""


# 全量菜单：type 1 滚球 / 3 今日；euid 40053 可返回多球种
# 精简：去掉早盘(type 4)和补强今日，减少请求轮次
MATCH_QUERY_SPECS = (
    {"euid": "40053", "type": 1},
    {"euid": "40053", "type": 3},
    {"euid": "40203", "type": 1},  # 足球滚球补强
)

# 仅滚球：实时比分/时钟/赔率轮询用
LIVE_QUERY_SPECS = (
    {"euid": "40053", "type": 1},
    {"euid": "40203", "type": 1},
)


def sanitize_token(token: str) -> str:
    return "".join(ch for ch in (token or "") if 33 <= ord(ch) < 127)


def decode_pb_data(data: Any) -> Any:
    if not isinstance(data, str) or not data:
        return data
    try:
        raw = base64.b64decode(data)
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


def ov_to_decimal(ov: Any) -> Optional[float]:
    """OB ov=127000 -> 1.27（亚洲盘）；若已是小数则原样；港盘区间自动换算。"""
    from app.services.odds_domain import coerce_float_european

    try:
        if ov is None or ov == "":
            return None
        v = float(ov)
        if v > 1000:
            v = v / 100000.0
        return coerce_float_european(v)
    except (TypeError, ValueError):
        return None


def parse_asian_line(hv: Any) -> Optional[float]:
    """将 '-0/0.5' / '+0.5' / '2' 转为数值。"""
    if hv is None:
        return None
    s = str(hv).strip().replace(" ", "")
    if not s:
        return None
    try:
        if "/" in s:
            sign = -1 if s.startswith("-") else 1
            body = s[1:] if s[0] in "+-" else s
            a, b = body.split("/", 1)
            return round(sign * (abs(float(a)) + abs(float(b))) / 2.0, 3)
        return float(s)
    except (TypeError, ValueError):
        return None


def _moneyline_from_hps(
    hps: list, *, mid: str = "", csid: str = "", tid: str = "", match_type: int = 1
) -> Optional[RemoteOdds]:
    for block in hps:
        if not isinstance(block, dict):
            continue
        if str(block.get("hpid")) != "1" and block.get("hpn") not in ("全场独赢", "独赢"):
            continue
        for line in block.get("hl") or []:
            if not isinstance(line, dict):
                continue
            odds_data: dict[str, Any] = {}
            selections: dict[str, dict] = {}
            hid = str(line.get("hid") or "")
            hpid = str(block.get("hpid") or "1")
            for ol in line.get("ol") or []:
                if not isinstance(ol, dict):
                    continue
                if ol.get("os") not in (1, "1", None, 0, "0"):
                    continue
                price = ov_to_decimal(ol.get("ov"))
                if price is None:
                    continue
                ot = str(ol.get("ot") or "")
                on = str(ol.get("on") or ol.get("onb") or "")
                key = None
                if ot == "1" or on in ("主胜", "主"):
                    key = "home"
                elif ot in ("2", "X2") or on in ("客胜", "客"):
                    key = "away"
                elif ot in ("X", "x") or on in ("平局", "平"):
                    key = "draw"
                if not key:
                    continue
                odds_data[key] = price
                selections[key] = {
                    "oid": str(ol.get("oid") or ""),
                    "hid": hid,
                    "ov": str(ol.get("ov") or ""),
                    "ot": ot,
                    "hpid": hpid,
                    "mid": mid,
                    "csid": csid,
                    "tid": tid,
                    "match_type": match_type,
                }
            if "home" in odds_data and "away" in odds_data:
                odds_data["_ob"] = {
                    "mid": mid,
                    "csid": csid,
                    "tid": tid,
                    "match_type": match_type,
                    "selections": selections,
                }
                return RemoteOdds(bet_type="moneyline", odds_data=odds_data)
    return None


def _spread_from_hps(
    hps: list, *, mid: str = "", csid: str = "", tid: str = "", match_type: int = 1
) -> Optional[RemoteOdds]:
    for block in hps:
        if not isinstance(block, dict):
            continue
        if str(block.get("hpid")) != "4" and "让球" not in str(block.get("hpn") or ""):
            continue
        for line in block.get("hl") or []:
            if not isinstance(line, dict):
                continue
            odds_data: dict[str, Any] = {}
            selections: dict[str, dict] = {}
            spread = parse_asian_line(line.get("hv")) or 0.0
            hid = str(line.get("hid") or "")
            hpid = str(block.get("hpid") or "4")
            for ol in line.get("ol") or []:
                if not isinstance(ol, dict):
                    continue
                price = ov_to_decimal(ol.get("ov"))
                if price is None:
                    continue
                ot = str(ol.get("ot") or "")
                key = "home" if ot == "1" else ("away" if ot == "2" else None)
                if not key:
                    continue
                odds_data[key] = price
                selections[key] = {
                    "oid": str(ol.get("oid") or ""),
                    "hid": hid,
                    "ov": str(ol.get("ov") or ""),
                    "ot": ot,
                    "hpid": hpid,
                    "mid": mid,
                    "csid": csid,
                    "tid": tid,
                    "match_type": match_type,
                }
            if "home" in odds_data and "away" in odds_data:
                odds_data["_ob"] = {
                    "mid": mid,
                    "csid": csid,
                    "tid": tid,
                    "match_type": match_type,
                    "selections": selections,
                }
                return RemoteOdds(bet_type="spread", odds_data=odds_data, spread=spread)
    return None


def _total_from_hps(
    hps: list, *, mid: str = "", csid: str = "", tid: str = "", match_type: int = 1
) -> Optional[RemoteOdds]:
    for block in hps:
        if not isinstance(block, dict):
            continue
        if str(block.get("hpid")) != "2" and "大小" not in str(block.get("hpn") or ""):
            continue
        for line in block.get("hl") or []:
            if not isinstance(line, dict):
                continue
            odds_data: dict[str, Any] = {}
            selections: dict[str, dict] = {}
            hv = str(line.get("hv") or "").strip()
            total = parse_asian_line(hv)
            if not total:
                # 线解析失败：跳过该盘口（0.0 占位会成为毒数据，结算端只能退本金）
                continue
            hid = str(line.get("hid") or "")
            hpid = str(block.get("hpid") or "2")
            # hs: 0 开盘；非 0 常为锁盘/关盘，跳过以免下单 0402008
            if str(line.get("hs") or "0") not in ("0", ""):
                continue
            for ol in line.get("ol") or []:
                if not isinstance(ol, dict):
                    continue
                if ol.get("os") not in (1, "1", None, 0, "0"):
                    continue
                price = ov_to_decimal(ol.get("ov"))
                if price is None:
                    continue
                ot = str(ol.get("ot") or "").lower()
                on = str(ol.get("on") or "")
                key = None
                if ot == "under" or on.startswith("小"):
                    key = "under"
                if not key:
                    continue
                odds_data[key] = price
                selections[key] = {
                    "oid": str(ol.get("oid") or ""),
                    "hid": hid,
                    "ov": str(ol.get("ov") or ""),
                    "ot": ot,
                    "hpid": hpid,
                    "hv": hv,
                    "mid": mid,
                    "csid": csid,
                    "tid": tid,
                    "match_type": match_type,
                }
            if "under" in odds_data:
                odds_data["_ob"] = {
                    "mid": mid,
                    "csid": csid,
                    "tid": tid,
                    "match_type": match_type,
                    "selections": selections,
                }
                return RemoteOdds(bet_type="total", odds_data=odds_data, total=total)
    return None


def _map_sport(csid: str, csna: str) -> str:
    if csid in SPORT_MAP:
        return SPORT_MAP[csid]
    if csna in SPORT_MAP:
        return SPORT_MAP[csna]
    name = (csna or "").strip()
    if "足球" in name:
        return "football"
    if "篮球" in name:
        return "basketball"
    return "other"


def parse_matches_pb(rows: list, *, limit: int = 800) -> list[RemoteMatch]:
    out: list[RemoteMatch] = []
    seen: set[str] = set()
    skipped_virtual = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        home = str(row.get("mhn") or "").strip()
        away = str(row.get("man") or "").strip()
        mid = str(row.get("mid") or "").strip()
        if not home or not away or not mid:
            continue
        if mid in seen:
            continue
        csid = str(row.get("csid") or "")
        csna = str(row.get("csna") or "")
        sport = _map_sport(csid, csna)
        if sport not in SUPPORTED_SPORTS:
            continue
        # 联赛名：完整名 + 简称一并参与虚拟判定（截图中 VS-/EAFC/PANDA/2K）
        tn = str(row.get("tn") or "").strip()
        tnjc = str(row.get("tnjc") or "").strip()
        league_raw = f"{tn} {tnjc}".strip() or "开云体育"
        league = (tn or tnjc or "开云体育")[:100]
        mcls = str(row.get("mcls") or "")
        # 足球/篮球虚拟盘：解析赔率前即丢弃，不采集任何数据
        if is_virtual_match(sport, league_raw, home, away, mcls):
            skipped_virtual += 1
            continue
        from app.services.bookmakers.china_match import is_china_match

        if is_china_match(league_raw, home, away, sport):
            skipped_virtual += 1
            continue
        hps = row.get("hps") or []
        if not isinstance(hps, list) or not hps:
            continue
        odds_list: list[RemoteOdds] = []
        tid = str(row.get("tid") or row.get("tnid") or "")
        ms = row.get("ms")
        match_type = 2 if str(ms) == "1" else 1
        ml = _moneyline_from_hps(hps, mid=mid, csid=csid, tid=tid, match_type=match_type)
        if ml:
            odds_list.append(ml)
        sp = _spread_from_hps(hps, mid=mid, csid=csid, tid=tid, match_type=match_type)
        if sp:
            odds_list.append(sp)
        tot = _total_from_hps(hps, mid=mid, csid=csid, tid=tid, match_type=match_type)
        if tot:
            odds_list.append(tot)
        if not odds_list:
            continue
        seen.add(mid)
        status = "live" if str(ms) == "1" else "upcoming"
        start = str(row.get("mgt") or "")
        home_score, away_score = _parse_msc_score(row.get("msc"))
        clock = _parse_ob_clock(row) if status == "live" else ""
        period = _parse_ob_period(row, sport=sport) if status == "live" else ""
        # 完场/取消不再当滚球
        mmp = str(row.get("mmp") or "").strip()
        if mmp == "12":  # 码表：12=取消 → 走取消结算（退本金），绝不能按比分判输赢
            status = "cancelled"
            clock = ""
        elif period == "完场" or mmp in ("8", "100", "999"):
            status = "finished"
            clock = ""
        out.append(
            RemoteMatch(
                external_id=f"ob:{mid}",
                sport=sport,
                league=league,
                home_team=home[:100],
                away_team=away[:100],
                start_time=start,
                status=status,
                venue="YBTY",
                odds_list=odds_list,
                home_score=home_score,
                away_score=away_score,
                clock=clock,
                period=period,
            )
        )
        if len(out) >= limit:
            break
    if skipped_virtual:
        logger.info("parse_matches_pb: skipped %s virtual football/basketball rows", skipped_virtual)
    from app.services.odds_domain import normalize_odds_data_to_european

    for m in out:
        cleaned: list[RemoteOdds] = []
        for o in m.odds_list or []:
            data = normalize_odds_data_to_european(o.odds_data)
            if any(not str(k).startswith("_") for k in data):
                cleaned.append(
                    RemoteOdds(bet_type=o.bet_type, odds_data=data, spread=o.spread, total=o.total)
                )
        m.odds_list = cleaned
    return [m for m in out if m.odds_list]


async def fetch_ybty_matches_odds(
    *,
    base_url: str,
    session_token: str,
    limit: int = 800,
    headed: bool = False,
    live_only: bool = False,
    page=None,
    refresh_first: bool = False,
    venue_url: str = "",
) -> list[RemoteMatch]:
    """Playwright 拉取开云体育盘口；live_only=True 时只拉滚球（更快）。

    page: 复用长连接页面（验证后的浏览器）；传入时不关闭。
    refresh_first: 拉取前先 refresh 当前页（与滚球轮询同频）。
    venue_url: 验证时保存的 H5 盘口 URL；长连接页掉回大厅时用于恢复。
    """
    token = sanitize_token(session_token)
    if not token or not base_url:
        return []

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.warning("Playwright missing; cannot fetch OB odds")
        return []

    base = base_url.rstrip("/")
    h5_url: Optional[str] = None
    rows: list = []
    query_specs = list(LIVE_QUERY_SPECS if live_only else MATCH_QUERY_SPECS)
    own_browser = page is None
    browser = None
    want_venue = (venue_url or "").strip()
    if not want_venue:
        try:
            from app.services.bookmakers.site_session import site_sessions

            sess = site_sessions.get(base)
            if sess and getattr(sess, "venue_url", ""):
                want_venue = str(sess.venue_url or "").strip()
        except Exception:
            pass

    def _sport_ctx_from_url(u: str) -> tuple[str, str, str]:
        """从场馆 URL 提取 (h5_url, sport_token, cuid)。

        排除裸根 API 端点（如 https://api.xxx.com/?token=...）：goto 它只会
        得到 JSON/空白页，恢复长连接时页面会被带到该地址卡死（实测）。
        """
        u = (u or "").strip()
        if not u or "token=" not in u:
            return "", "", ""
        low = u.lower()
        _p = urlparse(u)
        # 无条件排除裸根 API 端点（如 https://api.xxx.com/?token=...）：
        # goto 只会得到 JSON/空白页，恢复长连接时页面会被带到该地址卡死。
        # 注意：下方特征列表含 "token=" 使原 any 分支恒假（死代码），
        # 排除逻辑必须独立在此执行
        if (_p.path or "/") in ("/", "") and not (_p.fragment or ""):
            return "", "", ""
        # 综合站大厅带 token 的不算；场馆 H5 / yewu / app-h5 才算
        if not any(
            x in low
            for x in ("yewu", "app-h5", "zlshelves", "/#/match", "sportstype")
        ):
            # 仍允许显式 h5Url（常含 token= 与第三方域名）
            if "http" not in low:
                return "", "", ""
        qs = parse_qs(_p.query)
        sport_token = (qs.get("token") or [""])[0]
        session_id = (qs.get("sessionId") or [""])[0]
        cuid = ""
        m = re.match(r"^(\d{15,22})", session_id or "")
        if m:
            cuid = m.group(1)
        if not sport_token:
            return "", "", ""
        return u, sport_token, cuid

    def _push_uniq(items: list[str], value: Any) -> None:
        s = str(value or "").strip()
        if s and s not in items:
            items.append(s)

    async def _collect_page_urls(page) -> list[str]:
        urls: list[str] = []
        try:
            urls.append(page.url or "")
        except Exception:
            pass
        try:
            for fr in page.frames:
                try:
                    fu = fr.url or ""
                except Exception:
                    fu = ""
                if fu:
                    urls.append(fu)
        except Exception:
            pass
        # 从主文档 / iframe storage 挖出带 token 的场馆 H5 地址（OB 常见挂在 iframe）
        dig_js = """() => {
          const out = [];
          const push = (u) => {
            if (u && typeof u === 'string' && u.includes('token=') && out.indexOf(u) < 0) out.push(u);
          };
          try { push(location.href); } catch (e) {}
          try {
            for (const k of Object.keys(localStorage || {})) {
              const v = localStorage.getItem(k) || '';
              if (v.includes('token=') && v.startsWith('http')) push(v);
              if (v.includes('token=') && v.includes('yewu')) push(v);
              try {
                const j = JSON.parse(v);
                if (j && typeof j === 'object') {
                  for (const kk of ['url','h5Url','h5_url','venueUrl','launchUrl','sportUrl']) {
                    if (j[kk]) push(String(j[kk]));
                  }
                }
              } catch (e) {}
            }
          } catch (e) {}
          try {
            document.querySelectorAll('iframe').forEach((f) => {
              try { push(f.src); } catch (e) {}
            });
          } catch (e) {}
          return out.slice(0, 40);
        }"""
        try:
            found = await page.evaluate(dig_js)
            if isinstance(found, list):
                urls.extend([str(x) for x in found if x])
        except Exception:
            pass
        try:
            for fr in page.frames:
                try:
                    found = await fr.evaluate(dig_js)
                    if isinstance(found, list):
                        urls.extend([str(x) for x in found if x])
                except Exception:
                    continue
        except Exception:
            pass
        # 去重保序
        seen = set()
        uniq = []
        for u in urls:
            if u and u not in seen:
                seen.add(u)
                uniq.append(u)
        return uniq

    async def _discover_runtime_ctx(page) -> dict[str, Any]:
        probe_js = """() => {
          const data = {
            currentUrl: '',
            urls: [],
            tokens: [],
            sportTokens: [],
            sessionIds: [],
            apiHosts: [],
          };
          const push = (arr, val) => {
            if (!val) return;
            const s = String(val).trim();
            if (s && arr.indexOf(s) < 0) arr.push(s);
          };
          const pushHost = (val) => {
            try {
              const u = new URL(String(val), location.href);
              if (u.origin && data.apiHosts.indexOf(u.origin) < 0) data.apiHosts.push(u.origin);
            } catch (e) {}
          };
          const pushSportToken = (val) => {
            const s = String(val || '').trim();
            if (s && !s.includes('http') && data.sportTokens.indexOf(s) < 0) {
              data.sportTokens.push(s);
            }
          };
          const scanValue = (val, depth = 0) => {
            if (val == null || depth > 5) return;
            if (typeof val === 'string') {
              const s = val.trim();
              if (!s) return;
              if (s.includes('token=')) {
                push(data.urls, s);
                pushHost(s);
              }
              try {
                const j = JSON.parse(s);
                if (j && typeof j === 'object') scanValue(j, depth + 1);
              } catch (e) {}
              return;
            }
            if (Array.isArray(val)) {
              for (const item of val) scanValue(item, depth + 1);
              return;
            }
            if (typeof val !== 'object') return;
            for (const [k, v] of Object.entries(val)) {
              const key = String(k || '').toLowerCase();
              if (typeof v === 'string') {
                if ((key.includes('token') || key === 'requestid' || key === 'requestId') && !String(v).includes('http')) {
                  push(data.tokens, v);
                }
                if (key.includes('requestid') || key.includes('sporttoken')) {
                  pushSportToken(v);
                }
                if (key === 'sessionid' || key === 'sid') push(data.sessionIds, v);
                if (key.includes('url') || key.includes('venue') || key.includes('launch') || key.includes('sport')) {
                  if (String(v).includes('token=')) push(data.urls, v);
                  pushHost(v);
                }
              }
              scanValue(v, depth + 1);
            }
          };
          try {
            data.currentUrl = String(location.href || '');
            push(data.urls, data.currentUrl);
            pushHost(data.currentUrl);
          } catch (e) {}
          try {
            const stores = [localStorage, sessionStorage];
            for (const store of stores) {
              if (!store) continue;
              for (let i = 0; i < store.length; i++) {
                const key = store.key(i) || '';
                const val = store.getItem(key) || '';
                const low = key.toLowerCase();
                if ((low.includes('token') || low === 'requestid') && val && !val.includes('http')) {
                  push(data.tokens, val);
                }
                if (low.includes('requestid') || low.includes('sporttoken')) {
                  pushSportToken(val);
                }
                if (low === 'sessionid' && val) push(data.sessionIds, val);
                scanValue(val, 0);
              }
            }
          } catch (e) {}
          try {
            for (const e of (performance.getEntriesByType('resource') || [])) {
              const name = String(e.name || '');
              if (!name) continue;
              if (/matchesPB|betOrder|yewu|api\\.|rccg|zlshelves|kaiyun/i.test(name)) {
                pushHost(name);
                if (name.includes('token=')) push(data.urls, name);
              }
            }
          } catch (e) {}
          try {
            document.querySelectorAll('iframe').forEach((f) => {
              try {
                const src = String(f.src || '').trim();
                if (src) {
                  push(data.urls, src);
                  pushHost(src);
                }
              } catch (e) {}
            });
          } catch (e) {}
          return data;
        }"""
        urls: list[str] = []
        tokens: list[str] = []
        sport_tokens: list[str] = []
        session_ids: list[str] = []
        api_hosts: list[str] = []
        targets: list[Any] = []
        probe_samples: list[dict[str, Any]] = []
        raw_targets = [page]
        try:
            raw_targets.extend(list(getattr(page, "frames", []) or []))
        except Exception:
            pass
        for tgt in raw_targets:
            try:
                probe = await tgt.evaluate(probe_js)
            except Exception:
                continue
            if not isinstance(probe, dict):
                continue
            cur_url = str(probe.get("currentUrl") or "").strip()
            frame_relevant = any(
                x in cur_url.lower()
                for x in ("zlshelves", "yewu", "app-h5", "token=", "ybty", "match")
            )
            for u in probe.get("urls") or []:
                _push_uniq(urls, u)
            for t in probe.get("tokens") or []:
                _push_uniq(tokens, sanitize_token(str(t or "")))
            for t in probe.get("sportTokens") or []:
                _push_uniq(sport_tokens, sanitize_token(str(t or "")))
            for sid in probe.get("sessionIds") or []:
                _push_uniq(session_ids, sid)
            for host in probe.get("apiHosts") or []:
                hs = str(host or "").rstrip("/")
                if hs:
                    _push_uniq(api_hosts, hs)
            if frame_relevant or probe.get("tokens") or probe.get("urls") or probe.get("apiHosts"):
                if tgt not in targets:
                    targets.append(tgt)
            probe_samples.append(
                {
                    "url": cur_url[:140],
                    "tokens": len(probe.get("tokens") or []),
                    "urls": len(probe.get("urls") or []),
                    "api_hosts": len(probe.get("apiHosts") or []),
                }
            )
        if not targets:
            targets = [page]

        h5 = ""
        sport_token = ""
        cuid = ""
        for u in urls:
            hu, st, cu = _sport_ctx_from_url(u)
            if hu and not h5:
                h5 = hu
            if st and not sport_token:
                sport_token = st
            if st:
                _push_uniq(sport_tokens, st)
            if cu and not cuid:
                cuid = cu
            if h5 and sport_token and cuid:
                break
        # requestId / sportToken 属于场馆 API；X-API-TOKEN 则通常只是综合站
        # 登录令牌。优先前者，最后才以普通 token 兜底。
        for token_candidate in tokens:
            _push_uniq(sport_tokens, token_candidate)
        if sport_tokens:
            sport_token = sport_tokens[0]
        if not cuid:
            for sid in session_ids:
                m = re.match(r"^(\d{15,22})", str(sid or ""))
                if m:
                    cuid = m.group(1)
                    break
        for pref in ("https://api.937kddt.com", "https://api.rccg5fz.com"):
            _push_uniq(api_hosts, pref)
        api_hosts = sorted(
            api_hosts,
            key=lambda h: (
                0 if "api." in h.lower() else 1,
                0 if "937kddt" in h.lower() else 1,
                h,
            ),
        )[:6]
        return {
            "h5_url": h5,
            "sport_token": sport_token,
            "sport_tokens": sport_tokens[:4],
            "cuid": cuid,
            "api_hosts": api_hosts,
            "targets": targets,
            "probe_samples": probe_samples[:6],
        }

    def _decode_matches_payloads(payloads: Any) -> list[dict[str, Any]]:
        merged: dict[str, dict] = {}
        for payload in payloads or []:
            if not isinstance(payload, dict):
                continue
            decoded = decode_pb_data(payload.get("data"))
            if not isinstance(decoded, list):
                continue
            for row in decoded:
                if not isinstance(row, dict):
                    continue
                mid = str(row.get("mid") or "")
                if not mid:
                    continue
                prev = merged.get(mid)
                if prev is None or (row.get("hps") and not prev.get("hps")):
                    merged[mid] = row
        return list(merged.values())

    async def _fetch_matches_pb(
        page,
        sport_token: str,
        cuid: str,
        *,
        api_hosts: list[str] | None = None,
        eval_targets: list[Any] | None = None,
    ) -> list:
        js = """async ({ sportToken, cuid, specs, hosts }) => {
          const uniqHosts = [];
          const pushHost = (raw) => {
            if (!raw) return;
            try {
              const u = new URL(String(raw), location.href);
              if (u.origin && uniqHosts.indexOf(u.origin) < 0) uniqHosts.push(u.origin);
            } catch (e) {}
          };
          for (const host of (hosts || [])) pushHost(host);
          try { pushHost(location.origin); } catch (e) {}
          for (const host of ['https://api.937kddt.com', 'https://api.rccg5fz.com']) pushHost(host);
          const headers = {
            'content-type': 'application/json',
            'lang': 'zh',
            'requestid': sportToken,
          };
          const outs = [];
          for (const host of uniqHosts) {
            for (const path of ['/yewu11/v1/m/matchesPB', '/yewu13/v1/m/matchesPB']) {
              for (const spec of specs) {
                try {
                  const resp = await fetch(host + path + '?t=' + Date.now(), {
                    method: 'POST',
                    headers,
                    body: JSON.stringify({
                      cuid: cuid || '',
                      euid: String(spec.euid || ''),
                      type: Number(spec.type || 3),
                      sort: 1,
                      device: 'v2_h5_st',
                      hpsFlag: 1,
                      category: 1,
                    }),
                    credentials: 'include',
                  });
                  const json = await resp.json();
                  if (json && typeof json === 'object') outs.push(json);
                } catch (e) {
                  outs.push({ error: String(e), host, path, euid: String(spec.euid || '') });
                }
              }
            }
          }
          return outs;
        }"""
        targets = eval_targets or [page]
        for tgt in targets:
            try:
                payloads = await tgt.evaluate(
                    js,
                    {
                        "sportToken": sport_token,
                        "cuid": cuid,
                        "specs": query_specs,
                        "hosts": api_hosts or [],
                    },
                )
            except Exception:
                continue
            rows = _decode_matches_payloads(payloads)
            if rows:
                return rows
            # 仅记录接口状态和响应形状，帮助区分令牌失效、CORS 与无赛事；
            # 不输出 requestId、cookie 或响应正文。
            diagnostics: list[dict[str, Any]] = []
            for payload in payloads or []:
                if not isinstance(payload, dict):
                    continue
                entry = {
                    "host": str(payload.get("host") or "")[:80],
                    "path": str(payload.get("path") or "")[:48],
                    "error": bool(payload.get("error")),
                    "code": str(payload.get("code") or payload.get("status") or "")[:24],
                    "has_data": payload.get("data") is not None,
                }
                if entry not in diagnostics:
                    diagnostics.append(entry)
                if len(diagnostics) >= 6:
                    break
            if diagnostics:
                last = float(getattr(page, "_ob_matchespb_diag_at", 0.0) or 0.0)
                if time.monotonic() - last >= 30.0:
                    page._ob_matchespb_diag_at = time.monotonic()
                    logger.info("OB matchesPB empty response diagnostics=%s", diagnostics)
        return []

    async def _run_on_page(page) -> list:
        nonlocal h5_url, rows
        from app.services.bookmakers.browser_login import (
            apply_desktop_viewport,
            dismiss_h5_orient_tip,
        )

        # 中心钱包壳页无法可靠跨域重放 matchesPB；当 H5 自己发出请求时，
        # 直接捕获同源响应可避免 CORS/风控对接口重放的影响。
        captured_match_payloads = getattr(page, "_ob_matchespb_captures", None)
        if not isinstance(captured_match_payloads, list):
            captured_match_payloads = []
            page._ob_matchespb_captures = captured_match_payloads

            async def _capture_matches_response(resp) -> None:
                try:
                    if "matchespb" not in (resp.url or "").lower():
                        return
                    payload = await resp.json()
                    if isinstance(payload, dict):
                        captured_match_payloads.append(payload)
                        del captured_match_payloads[:-20]
                except Exception:
                    return

            try:
                page.on("response", _capture_matches_response)
            except Exception:
                pass
        else:
            captured_match_payloads.clear()

        async def _activate_gateway_and_capture() -> list:
            try:
                from app.services.bookmakers.venue_entry import activate_ob_gateway_live

                if not await activate_ob_gateway_live(page):
                    return []
                await page.wait_for_timeout(1600)
                return _decode_matches_payloads(captured_match_payloads)
            except Exception as e:
                logger.debug("OB gateway live activation skipped: %s", e)
                return []

        async def _try_fetch_current_ctx(*, log_label: str = "") -> list:
            nonlocal h5_url
            ctx = await _discover_runtime_ctx(page)
            st = str(ctx.get("sport_token") or "")
            token_candidates = [
                sanitize_token(str(item or ""))
                for item in (ctx.get("sport_tokens") or [st])
            ]
            token_candidates = [item for item in token_candidates if item]
            cu = str(ctx.get("cuid") or "")
            hu = str(ctx.get("h5_url") or "")
            if hu:
                h5_url = hu
                try:
                    from app.services.bookmakers.site_session import site_sessions

                    sess = site_sessions.get(base)
                    if sess:
                        sess.venue_url = hu
                except Exception:
                    pass
            if not token_candidates:
                return []
            if log_label:
                logger.info(
                    "OB odds: %s token_source hosts=%s probe=%s",
                    log_label,
                    ctx.get("api_hosts") or [],
                    ctx.get("probe_samples") or [],
                )
            try:
                await dismiss_h5_orient_tip(page)
            except Exception:
                pass
            for candidate in token_candidates:
                rows = await _fetch_matches_pb(
                    page,
                    candidate,
                    cu,
                    api_hosts=list(ctx.get("api_hosts") or []),
                    eval_targets=list(ctx.get("targets") or [page]),
                )
                if rows:
                    return rows
            last = float(getattr(page, "_ob_matchespb_empty_at", 0.0) or 0.0)
            if time.monotonic() - last >= 30.0:
                page._ob_matchespb_empty_at = time.monotonic()
                logger.info(
                    "OB matchesPB returned no rows token_candidates=%d has_cuid=%s",
                    len(token_candidates),
                    bool(cu),
                )
            return []

        # 主站必须桌面视口，否则会落到 APP 下载页
        await apply_desktop_viewport(page)

        # ---------- 优先：复用已手动进入的场馆页（禁止 goto 冲掉） ----------
        try:
            rows = await _try_fetch_current_ctx(log_label="reuse current venue (no navigate)")
            if rows:
                return rows
        except Exception as e:
            logger.debug("OB reuse current venue failed: %s", e)

        rows = await _activate_gateway_and_capture()
        if rows:
            logger.info("OB odds captured %d rows after H5 live activation", len(rows))
            return rows

        # 已有会话页：禁止 reload/乱跳；已在场馆则直接采数
        if not own_browser:
            try:
                from app.services.bookmakers.venue_entry import (
                    is_in_sportsbook,
                    page_already_on_live_board,
                )

                if await page_already_on_live_board(page) or await is_in_sportsbook(page):
                    rows = await _try_fetch_current_ctx(log_label="already in venue")
                    if rows:
                        return rows
            except Exception:
                pass
            # refresh_first 不再 reload 长连接页（易白屏乱跳）
            try:
                rows = await _try_fetch_current_ctx()
                if rows:
                    return rows
            except Exception:
                pass

            # 长连接页掉回综合站大厅：先恢复验证时保存的 H5 venue_url，再取真实滚球
            restored = False
            if want_venue:
                hu, st, cuid = _sport_ctx_from_url(want_venue)
                if st:
                    try:
                        from app.services.bookmakers.browser_login import apply_desktop_viewport as _adv

                        logger.info("OB odds: restore venue_url for live data")
                        await page.goto(want_venue, wait_until="domcontentloaded", timeout=45000)
                        await page.wait_for_timeout(1000)
                        await _adv(page)
                        try:
                            await dismiss_h5_orient_tip(page)
                        except Exception:
                            pass
                        rows = await _try_fetch_current_ctx(log_label="restore venue_url")
                        if rows:
                            restored = True
                            return rows
                        # URL 已含 token 但 frames 未就绪：直接用保存的 token 拉 API
                        h5_url = hu
                        restored = True
                        return await _fetch_matches_pb(page, st, cuid, api_hosts=["https://api.937kddt.com", "https://api.rccg5fz.com"])
                    except Exception as e:
                        logger.warning("OB restore venue_url failed: %s", e)

            # 仍在大厅：软进馆（注入 token → /game/sport → 点开云），否则滚球永远空
            try:
                from app.services.bookmakers.venue_entry import is_in_sportsbook

                in_book = await is_in_sportsbook(page)
            except Exception:
                in_book = False
            if not in_book and not restored:
                logger.warning(
                    "OB page not in sportsbook (url=%s) — soft re-enter for live odds",
                    (getattr(page, "url", "") or "")[:160],
                )
                try:
                    from app.services.bookmakers.browser_login import apply_desktop_viewport as _adv2

                    await _adv2(page)
                    await page.goto(base + "/", wait_until="domcontentloaded", timeout=45000)
                    await page.evaluate(
                        "(t) => { try { localStorage.setItem('X-API-TOKEN', t); } catch (e) {} }",
                        token,
                    )
                    await page.goto(base + "/game/sport", wait_until="domcontentloaded", timeout=60000)
                    await page.wait_for_timeout(1200)
                    await _adv2(page)
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
                    await page.wait_for_timeout(2500)
                    rows = await _try_fetch_current_ctx(log_label="soft re-enter")
                    if rows:
                        return rows
                except Exception as e:
                    logger.warning("OB soft re-enter failed: %s", e)

            probe = {}
            try:
                probe = await _discover_runtime_ctx(page)
            except Exception:
                probe = {}
            logger.warning(
                "OB venue page has no sport token after restore/re-enter; url=%s probe=%s",
                (getattr(page, "url", "") or "")[:160],
                probe.get("probe_samples") if isinstance(probe, dict) else [],
            )
            return []

        # ---------- 无长连接：才走自动进馆（会导航） ----------
        if refresh_first:
            try:
                await page.reload(wait_until="domcontentloaded", timeout=45000)
                await page.wait_for_timeout(600)
                await apply_desktop_viewport(page)
            except Exception:
                logger.debug("refresh_first reload failed, continue navigate")

        async def on_resp(resp):
            nonlocal h5_url
            try:
                if "venue/launch" not in resp.url:
                    return
                body = await resp.json()
                data = body.get("data") if isinstance(body, dict) else None
                if isinstance(data, dict):
                    url = (
                        data.get("pcUrl")
                        or data.get("url")
                        or data.get("h5Url")
                        or data.get("activityUrl")
                    )
                    if url and ("token=" in str(url) or "http" in str(url)):
                        h5_url = str(url)
            except Exception:
                return

        page.on("response", on_resp)

        try:
            nav_timeout = 45000 if live_only else 60000
            sport_timeout = 60000 if live_only else 90000
            await page.goto(base + "/", wait_until="domcontentloaded", timeout=nav_timeout)
            await page.evaluate(
                "(t) => { try { localStorage.setItem('X-API-TOKEN', t); } catch (e) {} }",
                token,
            )
            await page.goto(base + "/game/sport", wait_until="domcontentloaded", timeout=sport_timeout)
            await page.wait_for_timeout(1200 if live_only else 1500)
            await apply_desktop_viewport(page)

            clicked = False
            for label in ("开云体育", "ONE体育", "熊猫体育", "OB体育"):
                try:
                    loc = page.get_by_text(label, exact=True).first
                    if await loc.count() == 0:
                        loc = page.get_by_text(label, exact=False).first
                    if await loc.count() == 0:
                        continue
                    await loc.click(timeout=3000)
                    clicked = True
                    break
                except Exception:
                    continue
            if not clicked:
                logger.warning("OB sport venue entry not clicked")

            for _ in range(35 if live_only else 40):
                if h5_url:
                    break
                await page.wait_for_timeout(350)

            if not h5_url:
                logger.warning("OB venue launch h5Url not captured")
                return []

            await page.goto(h5_url, wait_until="domcontentloaded", timeout=60000 if live_only else 90000)
            await dismiss_h5_orient_tip(page)
            await page.wait_for_timeout(3500 if live_only else 5000)
            await dismiss_h5_orient_tip(page)

            _, sport_token, cuid = _sport_ctx_from_url(h5_url)
            if not sport_token:
                rows = await _try_fetch_current_ctx(log_label="own-browser h5 fallback")
                return rows
            rows = await _fetch_matches_pb(
                page,
                sport_token,
                cuid,
                api_hosts=["https://api.937kddt.com", "https://api.rccg5fz.com"],
                eval_targets=[page] + list(getattr(page, "frames", []) or []),
            )
            if rows:
                return rows
            return await _try_fetch_current_ctx(log_label="own-browser post-h5")
        except Exception:
            logger.exception("fetch_ybty_matches_odds failed")
            return []
        finally:
            try:
                page.remove_listener("response", on_resp)
            except Exception:
                pass

    if own_browser:
        from app.services.bookmakers.browser_login import (
            DESKTOP_UA,
            DESKTOP_VIEWPORT,
        )

        # 主站桌面身份；始终弹出可见浏览器。
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=False,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            context = await browser.new_context(
                ignore_https_errors=True,
                locale="zh-CN",
                viewport=dict(DESKTOP_VIEWPORT),
                user_agent=DESKTOP_UA,
            )
            page = await context.new_page()
            try:
                rows = await _run_on_page(page)
            finally:
                await browser.close()
    else:
        rows = await _run_on_page(page)

    parsed = parse_matches_pb(rows, limit=limit)
    by_sport: dict[str, int] = {}
    for m in parsed:
        by_sport[m.sport] = by_sport.get(m.sport, 0) + 1
    logger.info(
        "OB odds fetched: raw=%d parsed=%d sports=%s kept_page=%s",
        len(rows),
        len(parsed),
        by_sport,
        not own_browser,
    )
    return parsed


async def fetch_ob_live_odds(
    *,
    base_url: str,
    session_token: str,
    limit: int = 300,
    headed: bool = False,
    live_only: bool = False,
    page=None,
    refresh_first: bool = False,
    venue_url: str = "",
) -> list[RemoteMatch]:
    """体育(YBTY) 真实盘口：仅足球 / 篮球。"""
    sports = await fetch_ybty_matches_odds(
        base_url=base_url,
        session_token=session_token,
        limit=limit,
        headed=headed,
        live_only=live_only,
        page=page,
        refresh_first=refresh_first,
        venue_url=venue_url,
    )
    if live_only:
        sports = [m for m in sports if m.status == "live"]
    logger.info(
        "OB odds fetched: sports=%d live_only=%s",
        len(sports),
        live_only,
    )
    return sports[:limit] if limit and len(sports) > limit else sports
