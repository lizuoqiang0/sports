"""
捷报比分（nowscore.com）数据抓取器。

替代 DuckDuckGo 搜索，直接从捷报比分获取结构化赛前数据：
- 历史交锋 (H2H)
- 球队近期状态 (近10场)
- 球员伤病/停赛
- 联赛积分排名
- 战意/轮换（对阵前/后9名战绩、未来赛程）
- 球员级别数据（进球/失球/胜率）
- 天气/场地

数据来源：
  - /mvc/match/GetRecommendSchedules?sportType=1  获取今日比赛ID列表
  - /Analy/Analysis/{scheduleId}.htm              分析页（H2H、近况、盘路、赛程）
  - /mvc/soccer/qingbao?scheduleId={id}           情报页（积分排名、伤停）
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_BASE = settings.NOWSCORE_BASE_URL
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

# 代理：复用 NOWSCORE_PROXY_URL 或系统代理
_PROXY = None
_proxy_raw = str(getattr(settings, "NOWSCORE_PROXY_URL", "") or "").strip()
if _proxy_raw:
    _PROXY = _proxy_raw

# 共享 HTTP client，复用 TCP 连接
_shared_client: Optional[httpx.AsyncClient] = None

# 分析页 HTML 缓存 {schedule_id: html}，同一场比赛分析页单次分析周期内不变
_html_cache: dict[int, str] = {}


async def _get_client() -> httpx.AsyncClient:
    """获取共享的 httpx.AsyncClient，复用 TCP 连接。"""
    global _shared_client
    if _shared_client is None or _shared_client.is_closed:
        _shared_client = httpx.AsyncClient(
            timeout=15.0,
            headers={"User-Agent": _UA, "Referer": f"{_BASE}/"},
            proxy=_PROXY,
        )
    return _shared_client


# ── HTML 工具 ────────────────────────────────────────────────────────────

def _strip_html(s: str) -> str:
    """去 HTML 标签 + 压缩空白。"""
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _parse_tables(html: str) -> list[list[list[str]]]:
    """提取页面所有 table，返回 [[ [cell, cell, ...], ... ], ...]。"""
    tables: list[list[list[str]]] = []
    for tbl in re.findall(r"<table[^>]*>(.*?)</table>", html, re.S):
        rows: list[list[str]] = []
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", tbl, re.S):
            cells = [
                _strip_html(c)
                for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)
            ]
            if any(c for c in cells):
                rows.append(cells)
        if rows:
            tables.append(rows)
    return tables


def _parse_score(score_str: str) -> dict[str, Any]:
    """解析比分字符串 '2-1-1-0' -> {home_goals, away_goals, home_red, away_red}。
    格式: 比分-红牌-角球（如 '2-1-1-0-5-3'）或纯比分（如 '2-1'）。
    """
    # 处理 "半场 全场" 格式（空格分隔），取最后一组作为全场比分
    score_str = score_str.strip()
    if " " in score_str:
        groups = score_str.split()
        score_str = groups[-1] if groups else score_str
    parts = score_str.split("-")
    result: dict[str, Any] = {}
    if len(parts) >= 2:
        result["home_goals"] = int(parts[0]) if parts[0].isdigit() else None
        result["away_goals"] = int(parts[1]) if parts[1].isdigit() else None
    if len(parts) >= 4:
        result["home_red"] = int(parts[2]) if parts[2].isdigit() else 0
        result["away_red"] = int(parts[3]) if parts[3].isdigit() else 0
    if len(parts) >= 6:
        result["home_corners"] = int(parts[4]) if parts[4].isdigit() else 0
        result["away_corners"] = int(parts[5]) if parts[5].isdigit() else 0
    return result


# ── 比赛列表 & ID 查找 ──────────────────────────────────────────────────

# 缓存今日比赛标题列表 {timestamp, {scheduleId: "主队 vs 客队"}}
_title_cache: dict[str, Any] = {"ts": 0, "data": {}}
_CACHE_TTL = settings.NOWSCORE_TITLE_CACHE_TTL  # 30 分钟


async def _get_today_schedule_ids(sport_type: int = 1) -> list[int]:
    """获取今日推荐比赛 ID 列表。sport_type: 1=足球, 2=篮球。"""
    try:
        c = await _get_client()
        resp = await c.get(f"{_BASE}/mvc/match/GetRecommendSchedules?sportType={sport_type}")
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list):
                return [int(x) for x in data if isinstance(x, (int, str)) and str(x).isdigit()]
    except Exception as e:
        logger.warning("nowscore GetRecommendSchedules failed: %s", e)
    return []


async def _get_all_titles(sport: str = "football") -> dict[int, str]:
    """获取所有今日比赛标题（带缓存）。返回 {scheduleId: "主队 vs 客队"}。"""
    import time

    now = time.time()
    if _title_cache["data"] and now - _title_cache["ts"] < _CACHE_TTL:
        return _title_cache["data"]

    sport_type = 2 if "basket" in (sport or "").lower() else 1
    ids = await _get_today_schedule_ids(sport_type)
    if not ids:
        return {}

    # 并发获取所有标题（每批 20）
    titles: dict[int, str] = {}
    batch_size = settings.NOWSCORE_TITLE_BATCH_SIZE
    for i in range(0, len(ids), batch_size):
        batch = ids[i : i + batch_size]
        tasks = [_get_match_title(sid, sport) for sid in batch]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for sid, title in zip(batch, results):
            if isinstance(title, str) and title:
                titles[sid] = title

    _title_cache["ts"] = now
    _title_cache["data"] = titles
    logger.info("nowscore: cached %d match titles", len(titles))
    return titles


async def _find_schedule_id(
    home: str, away: str, league: str = "", sport: str = "football"
) -> Optional[int]:
    """通过球队名匹配今日比赛 ID。"""
    titles = await _get_all_titles(sport)
    if not titles:
        logger.warning("nowscore: no schedule IDs available")
        return None

    home_lower = home.lower().strip()
    away_lower = away.lower().strip()

    for sid, title in titles.items():
        title_lower = title.lower()
        # 1. 精确子串匹配
        if home_lower in title_lower and away_lower in title_lower:
            logger.info("nowscore: matched %s vs %s -> sid=%s", home, away, sid)
            return sid

    # 2. 模糊匹配：取名字中连续 2+ 字符进行匹配
    for sid, title in titles.items():
        if _fuzzy_team_match(home, title) and _fuzzy_team_match(away, title):
            logger.info("nowscore: fuzzy matched %s vs %s -> sid=%s", home, away, sid)
            return sid

    # 3. 单字匹配（中文球队名取每个字检查，过滤常见无意义字符）
    _COMMON_CHARS = set("FCfc队女()（）男女队足球俱乐部体育股份有限")
    for sid, title in titles.items():
        home_chars = [c for c in home if c.strip() and c not in _COMMON_CHARS]
        away_chars = [c for c in away if c.strip() and c not in _COMMON_CHARS]
        # 根据名字长度自适应阈值：长名字要求 3 字，短名字要求 2 字
        home_thresh = min(3, len(home_chars))
        away_thresh = min(3, len(away_chars))
        if home_thresh >= 2 and away_thresh >= 2:
            home_match = sum(1 for c in home_chars if c in title) >= home_thresh
            away_match = sum(1 for c in away_chars if c in title) >= away_thresh
            if home_match and away_match:
                logger.info("nowscore: char matched %s vs %s -> sid=%s", home, away, sid)
                return sid

    logger.warning("nowscore: no match found for %s vs %s", home, away)
    return None


def _fuzzy_team_match(team: str, title: str) -> bool:
    """模糊匹配：球队名前 4 个字符或任意 3 连续字符出现在标题中。"""
    if len(team) < 2:
        return False
    team_lower = team.lower()
    title_lower = title.lower()
    # 前 4 字符
    if team_lower[:4] in title_lower:
        return True
    # 任意 3 连续字符
    for i in range(len(team_lower) - 2):
        if team_lower[i : i + 3] in title_lower:
            return True
    return False


async def _get_match_title(schedule_id: int, sport: str = "football") -> str:
    """从分析页标题获取比赛信息。"""
    try:
        c = await _get_client()
        analysis_path = "/AnalyLq/Analysis/" if "basket" in (sport or "").lower() else "/Analy/Analysis/"
        resp = await c.get(f"{_BASE}{analysis_path}{schedule_id}.htm")
        if resp.status_code == 200:
            _html_cache[schedule_id] = resp.text
            m = re.search(r"<title>([^<]+)</title>", resp.text)
            if m:
                return m.group(1)
    except Exception:
        pass
    return ""


# ── 数据抓取 ────────────────────────────────────────────────────────────

async def _fetch_analysis_page(schedule_id: int, sport: str = "football") -> str:
    """获取分析页 HTML。"""
    cached = _html_cache.get(schedule_id)
    if cached:
        return cached
    try:
        c = await _get_client()
        analysis_path = "/AnalyLq/Analysis/" if "basket" in (sport or "").lower() else "/Analy/Analysis/"
        resp = await c.get(f"{_BASE}{analysis_path}{schedule_id}.htm")
        if resp.status_code == 200:
            _html_cache[schedule_id] = resp.text
            return resp.text
    except Exception as e:
        logger.warning("nowscore: analysis page fetch failed sid=%s err=%s", schedule_id, e)
    return ""


async def _fetch_qingbao_page(schedule_id: int, sport: str = "football") -> str:
    """获取情报页 HTML。"""
    try:
        c = await _get_client()
        if "basket" in (sport or "").lower():
            url = f"{_BASE}/AnalyLq/Shijian/{schedule_id}.htm"
        else:
            url = f"{_BASE}/mvc/soccer/qingbao?scheduleId={schedule_id}"
        resp = await c.get(url)
        if resp.status_code == 200:
            return resp.text
    except Exception as e:
        logger.warning("nowscore: qingbao page fetch failed sid=%s err=%s", schedule_id, e)
    return ""


# ── 数据解析 ────────────────────────────────────────────────────────────

_DATE_RE = re.compile(r"(\d{2,4})[-/](\d{1,2})[-/](\d{1,2})")


def _extract_date_from_cell(cell: str) -> tuple[str, str]:
    """从单元格中提取日期，处理"巴西杯 26-08-03"这种联赛+日期合并格式。
    返回 (date_str, league_str)。date_str 已归一化为 YYYY-MM-DD。
    """
    m = _DATE_RE.search(cell)
    if not m:
        return "", ""
    year, month, day = m.group(1), m.group(2), m.group(3)
    if len(year) == 2:
        year = "20" + year
    date_str = f"{year}-{int(month):02d}-{int(day):02d}"
    league_str = cell[:m.start()].strip()
    return date_str, league_str


def _parse_form_row(row: list[str]) -> Optional[dict[str, Any]]:
    """自适应解析近况/交锋表格行。支持3种布局：
    - 布局A(足球): [联赛+日期, 主队, 比分, 客队] -> date_idx=0, league非空, home=1, score=2, away=3
    - 布局B(足球): [联赛, 日期, 主队, 比分, 客队] -> date_idx=1, home=2, score=3, away=4
    - 布局C(篮球): [日期, 赛事, 主队, 比分, 客队] -> date_idx=0, league空, home=2, score=3, away=4
    返回 dict with date, competition, home, away, score, home_goals, away_goals
    """
    if not row or len(row) < 4:
        return None

    # 查找包含日期的单元格
    date_idx = -1
    date_str = ""
    league_from_cell = ""
    for i, cell in enumerate(row):
        ds, ls = _extract_date_from_cell(cell)
        if ds:
            date_idx = i
            date_str = ds
            league_from_cell = ls
            break

    if date_idx < 0:
        return None

    if league_from_cell:
        # 布局A: 联赛+日期合并，主队/比分/客队紧跟日期单元格
        competition = league_from_cell
        home = row[date_idx + 1].strip() if date_idx + 1 < len(row) else ""
        score = row[date_idx + 2].strip() if date_idx + 2 < len(row) else ""
        away = row[date_idx + 3].strip() if date_idx + 3 < len(row) else ""
    elif date_idx > 0:
        # 布局B: 联赛在日期前，主队/比分/客队紧跟日期单元格
        competition = row[date_idx - 1].strip()
        home = row[date_idx + 1].strip() if date_idx + 1 < len(row) else ""
        score = row[date_idx + 2].strip() if date_idx + 2 < len(row) else ""
        away = row[date_idx + 3].strip() if date_idx + 3 < len(row) else ""
    else:
        # 布局C: 日期在首位，赛事在第二列，主队/比分/客队在后面
        competition = row[1].strip() if len(row) > 1 else ""
        home = row[2].strip() if len(row) > 2 else ""
        score = row[3].strip() if len(row) > 3 else ""
        away = row[4].strip() if len(row) > 4 else ""

    if not home or not away or not score:
        return None

    score_data = _parse_score(score)
    return {
        "date": date_str,
        "competition": competition,
        "home": home,
        "away": away,
        "score": score,
        "home_goals": score_data.get("home_goals"),
        "away_goals": score_data.get("away_goals"),
    }


def _parse_h2h(html: str) -> dict[str, Any]:
    """解析历史交锋。从 HTML 中按日期格式提取 H2H 记录。"""
    matches: list[dict[str, Any]] = []
    tables = _parse_tables(html)

    # 方式1：用表格检测（近10场 或 日期+比分 表头）+ _parse_form_row 解析
    for tbl in tables:
        if not tbl or len(tbl) <= 3:
            continue
        header = " ".join(tbl[0]) if tbl[0] else ""
        if ("近10场" in header or ("日期" in header and "比分" in header)) and len(tbl) > 3:
            for row in tbl[1:]:
                parsed = _parse_form_row(row)
                if parsed:
                    matches.append(parsed)
            if matches:
                break

    # 方式2（回退）：正则匹配日期行
    if not matches:
        rows = re.findall(
            r"<tr[^>]*>(.*?\d{2,4}[-/]\d{1,2}[-/]\d{1,2}.*?)</tr>",
            html, re.S,
        )
        for row in rows:
            cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
            cells = [_strip_html(c) for c in cells]
            if cells:
                parsed = _parse_form_row(cells)
                if parsed:
                    matches.append(parsed)

    # 去重（取前 10 条）
    seen = set()
    unique = []
    for m in matches:
        key = f"{m['date']}_{m['home']}_{m['away']}"
        if key not in seen:
            seen.add(key)
            unique.append(m)

    if not unique:
        return {"matches": [], "note": "未找到历史交锋数据"}
    home_wins = sum(1 for m in unique if m.get("home_goals") is not None and m.get("away_goals") is not None and m["home_goals"] > m["away_goals"])
    draws = sum(1 for m in unique if m.get("home_goals") is not None and m.get("away_goals") is not None and m["home_goals"] == m["away_goals"])
    away_wins = sum(1 for m in unique if m.get("home_goals") is not None and m.get("away_goals") is not None and m["home_goals"] < m["away_goals"])
    return {
        "matches": unique[:10],
        "summary": {
            "played": len(unique),
            "home_wins": home_wins,
            "draws": draws,
            "away_wins": away_wins,
        },
    }


def _parse_recent_form(html: str, team_side: str, _tables: Optional[list] = None) -> dict[str, Any]:
    """解析球队近期战绩。Table 5 (主队) / Table 6 (客队)。"""
    tables = _tables if _tables is not None else _parse_tables(html)
    form_tables = []
    for tbl in tables:
        if not tbl:
            continue
        header = " ".join(tbl[0]) if tbl[0] else ""
        if ("近10场" in header or ("日期" in header and "比分" in header)) and len(tbl) > 3:
            form_tables.append(tbl)

    # form_tables[0] = H2H, form_tables[1] = 主队近况, form_tables[2] = 客队近况
    idx = 1 if team_side == "home" else 2
    if idx >= len(form_tables):
        return {"matches": [], "note": f"未找到{'主队' if team_side == 'home' else '客队'}近期战绩"}

    tbl = form_tables[idx]
    matches = []
    for row in tbl[1:]:
        parsed = _parse_form_row(row)
        if not parsed:
            continue
        home_goals = parsed.get("home_goals")
        away_goals = parsed.get("away_goals")
        result = ""
        if home_goals is not None and away_goals is not None:
            if team_side == "home":
                result = "胜" if home_goals > away_goals else ("平" if home_goals == away_goals else "负")
            else:
                result = "胜" if away_goals > home_goals else ("平" if away_goals == home_goals else "负")
        parsed["result"] = result
        matches.append(parsed)
    return {"matches": matches[:10]}


def _parse_injuries(html: str, _tables: Optional[list] = None) -> list[str]:
    """解析伤停信息。从情报页 table 中提取伤停/停赛球员。

    实际行格式为多列：
    - [number, (position)player_name, '', '']
    - ['', '', number, (position)player_name]
    """
    injuries = []
    tables = _tables if _tables is not None else _parse_tables(html)
    for tbl in tables:
        for row in tbl:
            for i, cell in enumerate(row):
                # 检查是否匹配 "(位置)球员名" 格式
                m = re.match(r"\(([\u4e00-\u9fff\w·]+)\)(.+)", cell)
                if m:
                    position, name = m.groups()
                    name = name.strip()
                    # 向前找相邻的纯数字 cell 作为球衣号
                    jersey = ""
                    if i > 0 and row[i - 1].strip().isdigit():
                        jersey = row[i - 1].strip()
                    # 检查行内是否有 "停赛" 关键词
                    context_text = " ".join(row)
                    status = "停赛" if "停赛" in context_text else "伤停"
                    if jersey:
                        injuries.append(f"{jersey} ({position}){name} - {status}")
                    else:
                        injuries.append(f"({position}){name} - {status}")
    # 去重
    seen = set()
    result = []
    for inj in injuries:
        if inj not in seen:
            seen.add(inj)
            result.append(inj)
    return result[:10]


def _parse_standings(html: str, qingbao_html: str, _analysis_tables: Optional[list] = None, _qingbao_tables: Optional[list] = None) -> dict[str, Any]:
    """解析积分排名。优先从 qingbao 页获取（更详细）。支持足球和篮球两种格式。"""

    def _try_parse(tables: list) -> dict[str, Any]:
        result = {"home": None, "away": None}
        for tbl in tables:
            if not tbl:
                continue
            all_text = " ".join(" ".join(r) for r in tbl)
            # 检测篮球：均得/均失
            is_basketball = "均得" in all_text or "均失" in all_text

            if is_basketball:
                current_team = None
                for row in tbl:
                    if not row:
                        continue
                    first = row[0].strip()
                    # 单元素行是球队名
                    if len(row) == 1 and first and first not in ("总", "主", "客") and "全场" not in first and "半场" not in first and "赛" not in first and "均得" not in first and "均失" not in first and not first.isdigit():
                        current_team = first
                        continue
                    # 篮球格式1 (WNBA): [总/主/客, 赛, 胜, 负, 均得, 均失, 净, 排名, 胜率]
                    if first in ("总", "主", "客") and len(row) >= 7:
                        data = {
                            "team": current_team or "",
                            "scope": first,
                            "played": _safe_int(row[1]) if len(row) > 1 else None,
                            "win": _safe_int(row[2]) if len(row) > 2 else None,
                            "draw": 0,
                            "lose": _safe_int(row[3]) if len(row) > 3 else None,
                            "goals_for": _safe_float(row[4]) if len(row) > 4 else None,
                            "goals_against": _safe_float(row[5]) if len(row) > 5 else None,
                            "points": None,
                            "win_rate": row[-1].strip() if row else None,
                        }
                        if data["scope"] == "总":
                            if result["home"] is None:
                                result["home"] = data
                            elif result["away"] is None:
                                result["away"] = data
                                break
                    # 篮球格式2 (PBA): [排名#, 球队名, 赛, 胜, 负, 均得, 均失, 净, 胜率]
                    elif first.isdigit() and len(row) >= 8 and row[1].strip() and not row[1].strip().isdigit():
                        data = {
                            "team": row[1].strip(),
                            "scope": "总",
                            "played": _safe_int(row[2]) if len(row) > 2 else None,
                            "win": _safe_int(row[3]) if len(row) > 3 else None,
                            "draw": 0,
                            "lose": _safe_int(row[4]) if len(row) > 4 else None,
                            "goals_for": _safe_float(row[5]) if len(row) > 5 else None,
                            "goals_against": _safe_float(row[6]) if len(row) > 6 else None,
                            "points": None,
                            "win_rate": row[-1].strip() if row else None,
                        }
                        if result["home"] is None:
                            result["home"] = data
                        elif result["away"] is None:
                            result["away"] = data
                            break
            else:
                # 足球格式（原逻辑）
                if "积分" not in all_text or "胜率" not in all_text:
                    continue
                current_team = None
                for row in tbl:
                    if not row:
                        continue
                    first = row[0].strip()
                    if len(row) == 1 and first and first not in ("总", "主", "客") and "全场" not in first and "半场" not in first and "赛" not in first:
                        current_team = first
                    elif first in ("总", "主", "客") and current_team and len(row) >= 8:
                        data = {
                            "team": current_team,
                            "scope": first,
                            "played": _safe_int(row[1]) if len(row) > 1 else None,
                            "win": _safe_int(row[2]) if len(row) > 2 else None,
                            "draw": _safe_int(row[3]) if len(row) > 3 else None,
                            "lose": _safe_int(row[4]) if len(row) > 4 else None,
                            "goals_for": _safe_int(row[5]) if len(row) > 5 else None,
                            "goals_against": _safe_int(row[6]) if len(row) > 6 else None,
                            "points": _safe_int(row[7]) if len(row) > 7 else None,
                            "win_rate": row[8].strip() if len(row) > 8 else None,
                        }
                        if data["scope"] == "总":
                            if result["home"] is None:
                                result["home"] = data
                            elif result["away"] is None:
                                result["away"] = data
                                break
            if result["home"] and result["away"]:
                break
        return result

    # 优先 qingbao_tables，未找到回退到 analysis_tables
    if _qingbao_tables is not None and _qingbao_tables:
        result = _try_parse(_qingbao_tables)
        if result["home"] and result["away"]:
            return result

    if _analysis_tables is not None and _analysis_tables:
        result = _try_parse(_analysis_tables)
        if result["home"] and result["away"]:
            return result

    # 最后回退到解析 HTML
    tables = _parse_tables(qingbao_html or html)
    return _try_parse(tables)


def _parse_motivation(html: str, _tables: Optional[list] = None) -> dict[str, Any]:
    """解析战意（对阵前/后9名战绩 + 未来赛程）。"""
    tables = _tables if _tables is not None else _parse_tables(html)
    result = {"home": "", "away": "", "notes": []}

    for tbl in tables:
        header = " ".join(tbl[0]) if tbl else ""
        if "本赛季" in header and "对阵" in "".join(tbl[1] if len(tbl) > 1 else []):
            for row in tbl[1:]:
                if len(row) >= 4:
                    desc = row[0].strip()
                    wins = row[1].strip()
                    draws = row[2].strip()
                    loses = row[3].strip()
                    if "主队" in desc:
                        result["home"] = f"{desc}: {wins}胜{draws}平{loses}负"
                        result["notes"].append(desc)
                    elif "客队" in desc:
                        result["away"] = f"{desc}: {wins}胜{draws}平{loses}负"
                        result["notes"].append(desc)

    # 未来赛程
    for tbl in tables:
        header = " ".join(tbl[0]) if tbl else ""
        if "间隔" in header:
            for row in tbl[1:4]:
                if len(row) >= 6:
                    result["notes"].append(f"后续: {row[0]} {row[2]} vs {row[4]} ({row[5]}后)")

    return result


def _parse_player_stats(html: str, _tables: Optional[list] = None) -> dict[str, Any]:
    """解析球员级别数据（球队整体进球/失球/胜率）。支持足球和篮球。"""
    tables = _tables if _tables is not None else _parse_tables(html)

    # 足球：找"进球"+"均进"表头
    for tbl in tables:
        if not tbl:
            continue
        header = " ".join(tbl[0]) if tbl[0] else ""
        if "球队" in header and "进球" in header and "均进" in header:
            home_stats = None
            away_stats = None
            for row in tbl[1:]:
                if len(row) >= 8:
                    stat = {
                        "team": row[0].strip(),
                        "played": _safe_int(row[1]),
                        "goals": _safe_int(row[2]),
                        "avg_goals": _safe_float(row[3]),
                        "conceded": _safe_int(row[4]),
                        "avg_conceded": _safe_float(row[5]),
                        "win_rate": row[6].strip(),
                        "draw_rate": row[7].strip(),
                    }
                    if home_stats is None:
                        home_stats = stat
                    else:
                        away_stats = stat
            return {"home": [home_stats] if home_stats else [], "away": [away_stats] if away_stats else []}

    # 篮球：从积分榜(含"均得"/"均失"的表)提取
    for tbl in tables:
        if not tbl:
            continue
        all_text = " ".join(" ".join(r) for r in tbl)
        if "均得" not in all_text and "均失" not in all_text:
            continue

        home_stats = None
        away_stats = None
        current_team = None
        for row in tbl:
            if not row:
                continue
            first = row[0].strip()
            # 单元素行是球队名
            if len(row) == 1 and first and first not in ("总", "主", "客") and "全场" not in first and "半场" not in first and "赛" not in first and "均得" not in first and "均失" not in first and not first.isdigit():
                current_team = first
                continue
            # 篮球格式1 (WNBA): [总/主/客, 赛, 胜, 负, 均得, 均失, 净, 排名, 胜率]
            if first in ("总", "主", "客") and len(row) >= 7:
                if first == "总":
                    stat = {
                        "team": current_team or "",
                        "played": _safe_int(row[1]) if len(row) > 1 else None,
                        "goals": None,
                        "avg_goals": _safe_float(row[4]) if len(row) > 4 else None,
                        "conceded": None,
                        "avg_conceded": _safe_float(row[5]) if len(row) > 5 else None,
                        "win_rate": row[-1].strip() if row else None,
                        "draw_rate": "0%",
                    }
                    if home_stats is None:
                        home_stats = stat
                    else:
                        away_stats = stat
            # 篮球格式2 (PBA): [排名#, 球队名, 赛, 胜, 负, 均得, 均失, 净, 胜率]
            elif first.isdigit() and len(row) >= 8 and row[1].strip() and not row[1].strip().isdigit():
                stat = {
                    "team": row[1].strip(),
                    "played": _safe_int(row[2]) if len(row) > 2 else None,
                    "goals": None,
                    "avg_goals": _safe_float(row[5]) if len(row) > 5 else None,
                    "conceded": None,
                    "avg_conceded": _safe_float(row[6]) if len(row) > 6 else None,
                    "win_rate": row[-1].strip() if row else None,
                    "draw_rate": "0%",
                }
                if home_stats is None:
                    home_stats = stat
                else:
                    away_stats = stat

        if home_stats or away_stats:
            return {"home": [home_stats] if home_stats else [], "away": [away_stats] if away_stats else []}

    return {"home": [], "away": [], "note": "未找到球员数据"}


def _safe_int(s: str) -> Optional[int]:
    try:
        return int(s.strip())
    except (ValueError, AttributeError):
        return None


def _safe_float(s: str) -> Optional[float]:
    try:
        return float(s.strip())
    except (ValueError, AttributeError):
        return None


# ── 主入口 ──────────────────────────────────────────────────────────────

async def fetch_match_context_via_nowscore(
    home_team: str,
    away_team: str,
    league: str = "",
    sport: str = "football",
) -> Optional[dict[str, Any]]:
    """从捷报比分获取赛前上下文数据。"""
    # 1. 找到 scheduleId
    schedule_id = await _find_schedule_id(home_team, away_team, league, sport)
    if not schedule_id:
        logger.warning("nowscore: scheduleId not found for %s vs %s", home_team, away_team)
        return None

    # 2. 并发获取分析页 + 情报页
    analysis_html, qingbao_html = await asyncio.gather(
        _fetch_analysis_page(schedule_id, sport),
        _fetch_qingbao_page(schedule_id, sport),
    )

    if not analysis_html and not qingbao_html:
        logger.warning("nowscore: both pages empty for sid=%s", schedule_id)
        return None

    # 3. 解析数据（预解析 tables 避免重复调用 _parse_tables）
    analysis_tables = _parse_tables(analysis_html)
    qingbao_tables = _parse_tables(qingbao_html)

    h2h = _parse_h2h(analysis_html)
    home_form = _parse_recent_form(analysis_html, "home", _tables=analysis_tables)
    away_form = _parse_recent_form(analysis_html, "away", _tables=analysis_tables)
    injuries = _parse_injuries(qingbao_html, _tables=qingbao_tables)
    standings = _parse_standings(analysis_html, qingbao_html, _analysis_tables=analysis_tables, _qingbao_tables=qingbao_tables)
    motivation = _parse_motivation(analysis_html, _tables=analysis_tables)
    player_stats = _parse_player_stats(analysis_html, _tables=analysis_tables)

    # 4. 构建返回
    ctx = {
        "source": "nowscore",
        "schedule_id": schedule_id,
        "sport": sport,
        "h2h": h2h,
        "home_form": home_form,
        "away_form": away_form,
        "news_injuries": injuries,
        "standings": standings,
        "motivation": motivation,
        "player_stats": player_stats,
    }

    # 5. 计算完整度
    dimensions_present = []
    if h2h.get("matches"):
        dimensions_present.append("h2h")
    if home_form.get("matches") or away_form.get("matches"):
        dimensions_present.append("home_form")
        dimensions_present.append("away_form")
    if injuries:
        dimensions_present.append("injuries")
    if standings.get("home") or standings.get("away"):
        dimensions_present.append("standings")
    if motivation.get("home") or motivation.get("away"):
        dimensions_present.append("motivation")
    if player_stats.get("home") or player_stats.get("away"):
        dimensions_present.append("player_stats")

    all_dims = ["h2h", "home_form", "away_form", "injuries", "player_stats", "motivation", "standings"]
    dimensions_missing = [d for d in all_dims if d not in dimensions_present]
    completeness = len(dimensions_present) / len(all_dims)

    ctx["dimensions_present"] = dimensions_present
    ctx["dimensions_missing"] = dimensions_missing
    ctx["quality"] = {"source": "nowscore", "completeness": completeness}

    logger.info(
        "nowscore context: home=%s away=%s sid=%s present=%s completeness=%.2f",
        home_team, away_team, schedule_id, dimensions_present, completeness,
    )

    return ctx


# ── 批量预取：一次性爬取当日所有比赛 ────────────────────────────────────

async def _parse_one_schedule(
    schedule_id: int,
    home_team: str = "",
    away_team: str = "",
    league: str = "",
    sport: str = "football",
) -> Optional[dict[str, Any]]:
    """解析单场比赛的完整上下文（复用已缓存的 HTML）。"""
    analysis_html = _html_cache.get(schedule_id, "")
    if not analysis_html:
        analysis_html = await _fetch_analysis_page(schedule_id, sport)
    qingbao_html = await _fetch_qingbao_page(schedule_id, sport)

    if not analysis_html and not qingbao_html:
        return None

    # 从标题解析球队名（如果没有传入）
    if not home_team or not away_team:
        m = re.search(r"<title>([^<]+)</title>", analysis_html or "")
        if m:
            title = m.group(1)
            # 清理标题：去掉日期前缀和后缀
            title = re.sub(r"\d+月\d+日\s*[:：]\s*", "", title)
            title = re.sub(r"数据分析.*$", "", title)
            title = title.strip()
            parts = re.split(r"\s*[Vv][Ss]\s*", title, maxsplit=1)
            if len(parts) == 2:
                home_team = parts[0].strip()
                away_team = parts[1].strip()

    if not home_team or not away_team:
        return None

    analysis_tables = _parse_tables(analysis_html)
    qingbao_tables = _parse_tables(qingbao_html)

    h2h = _parse_h2h(analysis_html)
    home_form = _parse_recent_form(analysis_html, "home", _tables=analysis_tables)
    away_form = _parse_recent_form(analysis_html, "away", _tables=analysis_tables)
    injuries = _parse_injuries(qingbao_html, _tables=qingbao_tables)
    standings = _parse_standings(analysis_html, qingbao_html, _analysis_tables=analysis_tables, _qingbao_tables=qingbao_tables)
    motivation = _parse_motivation(analysis_html, _tables=analysis_tables)
    player_stats = _parse_player_stats(analysis_html, _tables=analysis_tables)

    ctx = {
        "source": "nowscore",
        "schedule_id": schedule_id,
        "sport": sport,
        "h2h": h2h,
        "home_form": home_form,
        "away_form": away_form,
        "news_injuries": injuries,
        "standings": standings,
        "motivation": motivation,
        "player_stats": player_stats,
    }

    dimensions_present = []
    if h2h.get("matches"):
        dimensions_present.append("h2h")
    if home_form.get("matches") or away_form.get("matches"):
        dimensions_present.append("home_form")
        dimensions_present.append("away_form")
    if injuries:
        dimensions_present.append("injuries")
    if standings.get("home") or standings.get("away"):
        dimensions_present.append("standings")
    if motivation.get("home") or motivation.get("away"):
        dimensions_present.append("motivation")
    if player_stats.get("home") or player_stats.get("away"):
        dimensions_present.append("player_stats")

    all_dims = ["h2h", "home_form", "away_form", "injuries", "player_stats", "motivation", "standings"]
    dimensions_missing = [d for d in all_dims if d not in dimensions_present]
    completeness = len(dimensions_present) / len(all_dims)

    ctx["dimensions_present"] = dimensions_present
    ctx["dimensions_missing"] = dimensions_missing
    ctx["quality"] = {"source": "nowscore", "completeness": completeness}

    return ctx


async def prefetch_today_all_contexts(
    sport: str = "football",
    concurrency: int = 10,
) -> int:
    """批量预取当日所有比赛的上下文数据，写入 Redis + DB。

    Args:
        sport: "football" 或 "basketball"
        concurrency: 并发数

    Returns:
        成功缓存的数量
    """
    sport_type = 2 if "basket" in (sport or "").lower() else 1

    # 1. 获取今日所有 scheduleId（1 次 HTTP）
    ids = await _get_today_schedule_ids(sport_type)
    if not ids:
        logger.warning("nowscore prefetch: no schedule IDs for sport=%s", sport)
        return 0

    logger.info("nowscore prefetch: %d matches for sport=%s", len(ids), sport)

    # 2. 批量获取标题（会填充 _html_cache）
    titles = await _get_all_titles(sport)
    if not titles:
        logger.warning("nowscore prefetch: no titles fetched")
        return 0

    # 3. 并发解析所有比赛的上下文
    sem = asyncio.Semaphore(concurrency)
    success_count = 0
    total = len(titles)
    _progress_key = "nowscore:prefetch:progress"

    async def _update_progress(done: int):
        try:
            from app.core.cache import cache
            await cache.set_json(_progress_key, {
                "sport": sport, "total": total, "done": done,
                "started_at": int(_start_ts),
            }, ttl=120)
        except Exception:
            pass

    _start_ts = __import__('time').time()
    await _update_progress(0)

    async def _parse_and_save(sid: int, title: str):
        nonlocal success_count
        async with sem:
            try:
                # 从标题解析球队名
                home, away = "", ""
                if title:
                    # 清理标题：去掉日期前缀和后缀
                    clean = re.sub(r"\d+月\d+日\s*[:：]\s*", "", title)
                    clean = re.sub(r"数据分析.*$", "", clean)
                    clean = clean.strip()
                    parts = re.split(r"\s*[Vv][Ss]\s*", clean, maxsplit=1)
                    if len(parts) == 2:
                        home = parts[0].strip()
                        away = parts[1].strip()

                ctx = await _parse_one_schedule(sid, home, away, sport=sport)
                if not ctx:
                    return

                # 计算 fixture_key 并保存
                from app.services.fixture_key import fixture_key
                from app.services.match_context_store import save_context

                fk = fixture_key(sport, home, away)
                await save_context(fixture_key=fk, ctx=ctx, ttl_sec=None)
                success_count += 1
                await _update_progress(success_count)
            except Exception as e:
                logger.debug("nowscore prefetch sid=%s failed: %s", sid, e)

    tasks = [_parse_and_save(sid, title) for sid, title in titles.items()]
    await asyncio.gather(*tasks, return_exceptions=True)

    logger.info(
        "nowscore prefetch done: sport=%s total=%d cached=%d",
        sport, len(ids), success_count,
    )
    return success_count
