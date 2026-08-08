"""
捷报比分（nowscore.com）数据抓取器。

直接抓取并缓存：
- 分析页：积分排名、对赛往绩、近期战绩、联赛走势、伤停、战绩特征、数据对比
- 直播页：首发阵容、进失球概率、半场/全场胜负统计
- 走势页：各公司初指/赛前指数

最终上下文落 Redis，供 AI 只读实时盘口 + 基本面。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
import re
from typing import Any, Optional

import httpx

from app.config import settings
from app.services.bookmakers.sport_classify import classify_sport, normalize_sport, reject_sport_mismatch
from app.services.sports_data import compute_quality

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

# 分析页 HTML 缓存 {(sport, schedule_id): html}，避免足球/篮球互串
_html_cache: dict[tuple[str, int], str] = {}
_TRACKED_DIMENSIONS = (
    "h2h",
    "home_form",
    "away_form",
    "standings",
    "analysis",
    "live",
    "trend",
)


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
    # 捷报常见 "全场 半场" 格式，如 "2-1 2-0"；第一组是全场比分。
    score_str = re.sub(r"\s+", " ", (score_str or "").strip())
    score_pairs = re.findall(r"(\d{1,3})\s*-\s*(\d{1,3})", score_str)
    if " " in score_str and score_pairs:
        home_goals, away_goals = score_pairs[0]
        return {
            "home_goals": int(home_goals),
            "away_goals": int(away_goals),
        }
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

# 缓存今日比赛标题列表，按 sport 隔离，避免足球/篮球互串
_title_cache: dict[str, dict[str, Any]] = {}
_CACHE_TTL = settings.NOWSCORE_TITLE_CACHE_TTL  # 30 分钟


def _sport_cache_key(sport: str) -> str:
    return "basketball" if "basket" in (sport or "").lower() else "football"


def _html_cache_key(schedule_id: int, sport: str) -> tuple[str, int]:
    return (_sport_cache_key(sport), int(schedule_id))


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
    cache_key = _sport_cache_key(sport)
    cached = _title_cache.get(cache_key) or {}
    if cached.get("data") and now - float(cached.get("ts") or 0) < _CACHE_TTL:
        return cached["data"]

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

    _title_cache[cache_key] = {"ts": now, "data": titles}
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
            _html_cache[_html_cache_key(schedule_id, sport)] = resp.text
            m = re.search(r"<title>([^<]+)</title>", resp.text)
            if m:
                return m.group(1)
    except Exception:
        pass
    return ""


# ── 数据抓取 ────────────────────────────────────────────────────────────

async def _fetch_analysis_page(schedule_id: int, sport: str = "football") -> str:
    """获取分析页 HTML。"""
    cached = _html_cache.get(_html_cache_key(schedule_id, sport))
    if cached:
        return cached
    try:
        c = await _get_client()
        analysis_path = "/AnalyLq/Analysis/" if "basket" in (sport or "").lower() else "/Analy/Analysis/"
        resp = await c.get(f"{_BASE}{analysis_path}{schedule_id}.htm")
        if resp.status_code == 200:
            _html_cache[_html_cache_key(schedule_id, sport)] = resp.text
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


async def _fetch_live_page(schedule_id: int, sport: str = "football") -> str:
    """获取直播页 HTML。"""
    try:
        c = await _get_client()
        live_path = "/AnalyLq/ShiJian/" if "basket" in (sport or "").lower() else "/Analy/ShiJian/"
        for url in (
            f"{_BASE}{live_path}{schedule_id}.htm",
            f"{_BASE}/Analy/ShiJian/{schedule_id}.htm",
        ):
            resp = await c.get(url)
            if resp.status_code == 200 and resp.text:
                return resp.text
    except Exception as e:
        logger.warning("nowscore: live page fetch failed sid=%s err=%s", schedule_id, e)
    return ""


async def _fetch_trend_page(schedule_id: int, sport: str = "football") -> str:
    """获取走势/初指页 HTML。"""
    try:
        c = await _get_client()
        candidates = [
            f"{_BASE}/Analy/Analysis/{schedule_id}.htm",
            f"{_BASE}/AnalyLq/Analysis/{schedule_id}.htm",
        ]
        for url in candidates:
            resp = await c.get(url)
            if resp.status_code == 200 and resp.text and (
                "赛前指数" in resp.text or "初指" in resp.text or "公司" in resp.text
            ):
                return resp.text
    except Exception as e:
        logger.warning("nowscore: trend page fetch failed sid=%s err=%s", schedule_id, e)
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


def _norm_team_name(name: str) -> str:
    s = (name or "").strip().lower()
    s = re.sub(r"\s+", "", s)
    s = re.sub(r"[()（）\-\.\u200e\u200f·'`]", "", s)
    return s


def _same_team_name(a: str, b: str) -> bool:
    na = _norm_team_name(a)
    nb = _norm_team_name(b)
    if not na or not nb:
        return False
    return na == nb or na in nb or nb in na


def _resolve_team_perspective(parsed: dict[str, Any], target_team: str) -> Optional[str]:
    if not target_team:
        return None
    if _same_team_name(parsed.get("home") or "", target_team):
        return "home"
    if _same_team_name(parsed.get("away") or "", target_team):
        return "away"
    return None


def _sync_context_dimensions(ctx: dict[str, Any]) -> dict[str, Any]:
    quality = compute_quality(ctx)
    present = [
        str(x).strip()
        for x in (quality.get("fields_present") or [])
        if str(x).strip() in _TRACKED_DIMENSIONS
    ]
    # analysis / live / trend 不在 compute_quality 的 fields_present 中，
    # 需依据 ctx 中的实际内容判定是否呈现，避免新增维度被永久归入 missing
    analysis = ctx.get("analysis") if isinstance(ctx, dict) else None
    if isinstance(analysis, dict) and (
        analysis.get("injuries")
        or analysis.get("features")
        or analysis.get("compare")
        or analysis.get("analysis_tables")
    ):
        if "analysis" not in present:
            present.append("analysis")
    live = ctx.get("live") if isinstance(ctx, dict) else None
    if isinstance(live, dict) and (
        (live.get("lineup") or {}).get("count")
        or (live.get("probabilities") or {}).get("count")
        or (live.get("half_full_stats") or {}).get("count")
        or live.get("tables")
    ):
        if "live" not in present:
            present.append("live")
    trend = ctx.get("trend") if isinstance(ctx, dict) else None
    if isinstance(trend, dict) and (trend.get("tables") or trend.get("initial_odds")):
        if "trend" not in present:
            present.append("trend")
    missing = [field for field in _TRACKED_DIMENSIONS if field not in set(present)]
    return {
        **ctx,
        "dimensions_present": present,
        "dimensions_missing": missing,
        "quality": quality,
    }


def _build_context_text(
    *,
    title: str,
    h2h: dict[str, Any],
    home_form: dict[str, Any],
    away_form: dict[str, Any],
    standings: dict[str, Any],
) -> str:
    chunks = [title]
    for bucket in (h2h, home_form, away_form):
        for row in (bucket.get("matches") or [])[:6]:
            if not isinstance(row, dict):
                continue
            chunks.extend(
                [
                    str(row.get("competition") or ""),
                    str(row.get("home") or ""),
                    str(row.get("away") or ""),
                    str(row.get("score") or ""),
                ]
            )
    for side in ("home", "away"):
        item = standings.get(side) or {}
        if isinstance(item, dict):
            chunks.extend([str(item.get("team") or ""), str(item.get("win_rate") or "")])
    return " ".join(x for x in chunks if x)


def _sample_score_pair(*buckets: dict[str, Any]) -> tuple[Optional[int], Optional[int]]:
    for bucket in buckets:
        for row in (bucket.get("matches") or []):
            if not isinstance(row, dict):
                continue
            home_goals = row.get("home_goals")
            away_goals = row.get("away_goals")
            if isinstance(home_goals, int) and isinstance(away_goals, int):
                return home_goals, away_goals
    return None, None


def _context_matches_declared_sport(
    *,
    sport: str,
    title: str,
    h2h: dict[str, Any],
    home_form: dict[str, Any],
    away_form: dict[str, Any],
    standings: dict[str, Any],
) -> bool:
    declared = normalize_sport(sport)
    if not declared:
        return False
    text = _build_context_text(
        title=title,
        h2h=h2h,
        home_form=home_form,
        away_form=away_form,
        standings=standings,
    )
    sample_home, sample_away = _sample_score_pair(h2h, home_form, away_form)
    inferred = classify_sport(
        text=text,
        home_score=sample_home or 0,
        away_score=sample_away or 0,
        sport_hint=declared,
    )
    if inferred and inferred != declared:
        return False
    if sample_home is None or sample_away is None:
        return True
    return not reject_sport_mismatch(
        declared,
        home_score=sample_home,
        away_score=sample_away,
        text=text,
    )


def _parse_h2h(html: str, home_team: str = "", away_team: str = "") -> dict[str, Any]:
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
    home_wins = 0
    draws = 0
    away_wins = 0
    for row in unique:
        home_goals = row.get("home_goals")
        away_goals = row.get("away_goals")
        if home_goals is None or away_goals is None:
            continue
        row_home = _resolve_team_perspective(row, home_team)
        row_away = _resolve_team_perspective(row, away_team)
        if row_home == "home" and row_away == "away":
            if home_goals > away_goals:
                home_wins += 1
            elif home_goals == away_goals:
                draws += 1
            else:
                away_wins += 1
        elif row_home == "away" and row_away == "home":
            if away_goals > home_goals:
                home_wins += 1
            elif home_goals == away_goals:
                draws += 1
            else:
                away_wins += 1
        else:
            if home_goals > away_goals:
                home_wins += 1
            elif home_goals == away_goals:
                draws += 1
            else:
                away_wins += 1
    return {
        "matches": unique[:10],
        "summary": {
            "played": len(unique),
            "home_wins": home_wins,
            "draws": draws,
            "away_wins": away_wins,
        },
    }


def _parse_recent_form(
    html: str,
    team_side: str,
    *,
    team_name: str = "",
    _tables: Optional[list] = None,
) -> dict[str, Any]:
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
            perspective = _resolve_team_perspective(parsed, team_name)
            if perspective == "home":
                result = "胜" if home_goals > away_goals else ("平" if home_goals == away_goals else "负")
            elif perspective == "away":
                result = "胜" if away_goals > home_goals else ("平" if away_goals == home_goals else "负")
            elif team_side == "home":
                result = "胜" if home_goals > away_goals else ("平" if home_goals == away_goals else "负")
            else:
                result = "胜" if away_goals > home_goals else ("平" if away_goals == home_goals else "负")
        parsed["result"] = result
        matches.append(parsed)
    return {"matches": matches[:10]}


def _parse_standings(
    html: str,
    qingbao_html: str,
    _analysis_tables: Optional[list] = None,
    _qingbao_tables: Optional[list] = None,
    *,
    home_team: str = "",
    away_team: str = "",
) -> dict[str, Any]:
    """解析积分排名。优先从 qingbao 页获取（更详细）。支持足球和篮球两种格式。"""

    def _normalize_row_candidate(
        row: list[str],
        *,
        is_basketball: bool,
        current_team: str = "",
    ) -> Optional[dict[str, Any]]:
        cells = [str(c).strip() for c in row if str(c).strip()]
        if not cells:
            return None
        if cells[0] in {"#", "球队", "排名"}:
            return None

        team = ""
        played = win = draw = lose = points = None
        goals_for = goals_against = None
        win_rate = ""
        scope = ""

        if len(cells) >= 9 and not cells[0].isdigit() and cells[0] not in ("总", "主", "客"):
            team = cells[0]
            played = _safe_int(cells[1])
            win = _safe_int(cells[2])
            draw = _safe_int(cells[3])
            lose = _safe_int(cells[4])
            goals_for = _safe_float(cells[5])
            goals_against = _safe_float(cells[6])
            points = _safe_int(cells[7])
            win_rate = cells[8]
        elif len(cells) >= 8 and cells[0].isdigit() and not cells[1].isdigit():
            team = cells[1]
            played = _safe_int(cells[2])
            win = _safe_int(cells[3])
            draw = 0 if not is_basketball else None
            lose = _safe_int(cells[4])
            goals_for = _safe_float(cells[5])
            goals_against = _safe_float(cells[6])
            win_rate = cells[8] if len(cells) > 8 else cells[-1]
        elif len(cells) >= 7 and cells[0] in ("总", "主", "客"):
            scope = cells[0]
            team = current_team or (cells[1] if len(cells) > 1 else "")
            if current_team:
                played = _safe_int(cells[1]) if len(cells) > 1 else None
                win = _safe_int(cells[2]) if len(cells) > 2 else None
                lose = _safe_int(cells[3]) if len(cells) > 3 else None
                goals_for = _safe_float(cells[4]) if len(cells) > 4 else None
                goals_against = _safe_float(cells[5]) if len(cells) > 5 else None
                win_rate = cells[-1]
            else:
                played = _safe_int(cells[2]) if len(cells) > 2 else None
                win = _safe_int(cells[3]) if len(cells) > 3 else None
                lose = _safe_int(cells[4]) if len(cells) > 4 else None
                goals_for = _safe_float(cells[5]) if len(cells) > 5 else None
                goals_against = _safe_float(cells[6]) if len(cells) > 6 else None
                win_rate = cells[-1]
        elif len(cells) >= 6 and not cells[0].isdigit():
            team = cells[0]
            played = _safe_int(cells[1])
            win = _safe_int(cells[2])
            if len(cells) >= 9:
                draw = _safe_int(cells[3])
                lose = _safe_int(cells[4])
                goals_for = _safe_float(cells[5])
                goals_against = _safe_float(cells[6])
                points = _safe_int(cells[7])
                win_rate = cells[8]
            else:
                lose = _safe_int(cells[3])
                goals_for = _safe_float(cells[4])
                goals_against = _safe_float(cells[5])
                win_rate = cells[-1]
        else:
            return None

        if not team:
            return None
        return {
            "team": team,
            "scope": scope,
            "played": played,
            "win": win,
            "draw": draw,
            "lose": lose,
            "goals_for": goals_for,
            "goals_against": goals_against,
            "points": points,
            "win_rate": win_rate,
        }

    def _assign(result: dict[str, Any], cand: dict[str, Any], *, home_team: str, away_team: str) -> None:
        team = str(cand.get("team") or "").strip()
        if not team:
            return
        if result["home"] is None and home_team and _same_team_name(team, home_team):
            result["home"] = cand
            return
        if result["away"] is None and away_team and _same_team_name(team, away_team):
            result["away"] = cand
            return
        if result["home"] is None:
            result["home"] = cand
            return
        if result["away"] is None and not _same_team_name(team, str((result["home"] or {}).get("team") or "")):
            result["away"] = cand

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
                    cand = _normalize_row_candidate(
                        row,
                        is_basketball=True,
                        current_team=current_team or "",
                    )
                    if cand:
                        _assign(result, cand, home_team=home_team, away_team=away_team)
            else:
                if "积分" not in all_text and "胜率" not in all_text and "均进" not in all_text:
                    continue
                for row in tbl:
                    if not row:
                        continue
                    cand = _normalize_row_candidate(row, is_basketball=False)
                    if cand:
                        _assign(result, cand, home_team=home_team, away_team=away_team)
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


def _table_text(tbl: list[list[str]]) -> str:
    return " ".join(" ".join(row) for row in tbl if row)


def _row_is_header(row: list[str]) -> bool:
    txt = " ".join(row)
    return any(k in txt for k in ("排名", "积分", "首发", "阵容", "进失球", "半场", "全场", "初指", "赛前指数", "对赛", "伤停", "走势", "战绩", "对比"))


def _first_non_empty(rows: list[list[str]]) -> list[str]:
    for row in rows:
        if any(str(c).strip() for c in row):
            return row
    return []


def _tables_with_keywords(tables: list[list[list[str]]], keywords: tuple[str, ...]) -> list[list[list[str]]]:
    out = []
    for tbl in tables:
        txt = _table_text(tbl)
        if any(k in txt for k in keywords):
            out.append(tbl)
    return out


def _rows_from_tables(tables: list[list[list[str]]]) -> list[list[str]]:
    rows: list[list[str]] = []
    for tbl in tables:
        rows.extend(tbl)
    return rows


def _section_tables(html: str, keywords: tuple[str, ...]) -> list[list[list[str]]]:
    tables = _parse_tables(html)
    return _tables_with_keywords(tables, keywords)


def _parse_named_rows(tables: list[list[list[str]]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for tbl in tables:
        if not tbl:
            continue
        header = _first_non_empty(tbl[:1]) or []
        out.append(
            {
                "header": header,
                "rows": tbl[1:] if len(tbl) > 1 else [],
                "raw": tbl,
                "text": _table_text(tbl),
            }
        )
    return out


def _parse_odds_trend_tables(html: str) -> dict[str, Any]:
    tables = _tables_with_keywords(_parse_tables(html), ("赛前指数", "初指", "初盘", "公司", "盘口"))
    if not tables:
        return {"tables": [], "initial_odds": []}
    return {
        "tables": _parse_named_rows(tables),
        "initial_odds": _parse_named_rows(tables[:4]),
        "note": "走势/初指数据已抓取",
    }


def _parse_injuries_and_features(html: str) -> dict[str, Any]:
    tables = _parse_tables(html)
    injuries = _tables_with_keywords(tables, ("伤停", "伤缺", "停赛"))
    features = _tables_with_keywords(tables, ("战绩特征", "数据对比", "联赛走势"))
    compare = _tables_with_keywords(tables, ("数据对比", "对比"))
    return {
        "injuries": _parse_named_rows(injuries),
        "features": _parse_named_rows(features),
        "compare": _parse_named_rows(compare),
    }


def _parse_live_data(html: str) -> dict[str, Any]:
    tables = _parse_tables(html)
    lineup_tbls = _tables_with_keywords(tables, ("首发", "阵容", "预计首发", "替补"))
    prob_tbls = _tables_with_keywords(tables, ("进失球概率", "进球概率", "失球概率"))
    hf_tbls = _tables_with_keywords(tables, ("半场", "全场", "胜负统计", "半全场"))

    def _maybe_team_block(tbls: list[list[list[str]]]) -> dict[str, Any]:
        named = _parse_named_rows(tbls)
        return {
            "home": named[0] if len(named) > 0 else None,
            "away": named[1] if len(named) > 1 else None,
            "tables": named,
            "count": len(named),
        }

    return {
        "lineup": _maybe_team_block(lineup_tbls),
        "probabilities": _maybe_team_block(prob_tbls),
        "half_full_stats": _maybe_team_block(hf_tbls),
        "tables": _parse_named_rows(lineup_tbls + prob_tbls + hf_tbls),
    }


def _build_context_payload(
    *,
    schedule_id: int,
    sport: str,
    home_team: str,
    away_team: str,
    title: str,
    analysis_html: str,
    qingbao_html: str,
    live_html: str,
    trend_html: str,
) -> Optional[dict[str, Any]]:
    analysis_tables = _parse_tables(analysis_html)
    qingbao_tables = _parse_tables(qingbao_html)
    h2h = _parse_h2h(analysis_html, home_team, away_team)
    home_form = _parse_recent_form(analysis_html, "home", team_name=home_team, _tables=analysis_tables)
    away_form = _parse_recent_form(analysis_html, "away", team_name=away_team, _tables=analysis_tables)
    standings = _parse_standings(
        analysis_html,
        qingbao_html,
        _analysis_tables=analysis_tables,
        _qingbao_tables=qingbao_tables,
        home_team=home_team,
        away_team=away_team,
    )
    analysis_extra = _parse_injuries_and_features(analysis_html)
    live_data = _parse_live_data(live_html)
    trend_data = _parse_odds_trend_tables(trend_html or analysis_html)

    if not _context_matches_declared_sport(
        sport=sport,
        title=title,
        h2h=h2h,
        home_form=home_form,
        away_form=away_form,
        standings=standings,
    ):
        logger.warning(
            "nowscore context rejected by sport check: sport=%s sid=%s home=%s away=%s title=%s",
            sport, schedule_id, home_team, away_team, title,
        )
        return None

    ctx = {
        "source": "nowscore",
        "schedule_id": schedule_id,
        "sport": sport,
        "home_team": home_team,
        "away_team": away_team,
        "match_title": title,
        "analysis": {
            "analysis_tables": _parse_named_rows(_section_tables(analysis_html, ("伤停", "战绩特征", "数据对比", "联赛走势"))),
            "injuries": analysis_extra.get("injuries") or [],
            "features": analysis_extra.get("features") or [],
            "compare": analysis_extra.get("compare") or [],
        },
        "live": live_data,
        "trend": trend_data,
        "h2h": h2h,
        "home_form": home_form,
        "away_form": away_form,
        "standings": standings,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    ctx = _sync_context_dimensions(ctx)
    if not (ctx.get("dimensions_present") or []):
        return None
    return ctx


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

    # 2. 并发获取分析页 + 情报页 + 直播页 + 走势页
    analysis_html, qingbao_html, live_html, trend_html = await asyncio.gather(
        _fetch_analysis_page(schedule_id, sport),
        _fetch_qingbao_page(schedule_id, sport),
        _fetch_live_page(schedule_id, sport),
        _fetch_trend_page(schedule_id, sport),
    )

    if not analysis_html and not qingbao_html:
        logger.warning("nowscore: both pages empty for sid=%s", schedule_id)
        return None

    # 3. 解析数据（预解析 tables 避免重复调用 _parse_tables）
    analysis_tables = _parse_tables(analysis_html)
    qingbao_tables = _parse_tables(qingbao_html)

    h2h = _parse_h2h(analysis_html, home_team, away_team)
    home_form = _parse_recent_form(analysis_html, "home", team_name=home_team, _tables=analysis_tables)
    away_form = _parse_recent_form(analysis_html, "away", team_name=away_team, _tables=analysis_tables)
    standings = _parse_standings(
        analysis_html,
        qingbao_html,
        _analysis_tables=analysis_tables,
        _qingbao_tables=qingbao_tables,
        home_team=home_team,
        away_team=away_team,
    )

    title = await _get_match_title(schedule_id, sport)
    if not _context_matches_declared_sport(
        sport=sport,
        title=title,
        h2h=h2h,
        home_form=home_form,
        away_form=away_form,
        standings=standings,
    ):
        logger.warning(
            "nowscore context rejected by sport check: sport=%s sid=%s home=%s away=%s title=%s",
            sport, schedule_id, home_team, away_team, title,
        )
        return None

    # 解析额外维度
    analysis_extra = _parse_injuries_and_features(analysis_html)
    live_data = _parse_live_data(live_html)
    trend_data = _parse_odds_trend_tables(trend_html or analysis_html)

    # 4. 构建返回
    ctx = {
        "source": "nowscore",
        "schedule_id": schedule_id,
        "sport": sport,
        "home_team": home_team,
        "away_team": away_team,
        "match_title": title,
        "analysis": {
            "injuries": analysis_extra.get("injuries") or [],
            "features": analysis_extra.get("features") or [],
            "compare": analysis_extra.get("compare") or [],
        },
        "live": live_data,
        "trend": trend_data,
        "h2h": h2h,
        "home_form": home_form,
        "away_form": away_form,
        "standings": standings,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    ctx = _sync_context_dimensions(ctx)
    if not (ctx.get("dimensions_present") or []):
        logger.warning(
            "nowscore context empty after parse: sport=%s sid=%s home=%s away=%s",
            sport, schedule_id, home_team, away_team,
        )
        return None

    logger.info(
        "nowscore context: home=%s away=%s sid=%s present=%s completeness=%.2f",
        home_team,
        away_team,
        schedule_id,
        ctx.get("dimensions_present") or [],
        float((ctx.get("quality") or {}).get("completeness") or 0.0),
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
    analysis_html = _html_cache.get(_html_cache_key(schedule_id, sport), "")
    if not analysis_html:
        analysis_html = await _fetch_analysis_page(schedule_id, sport)
    qingbao_html, live_html, trend_html = await asyncio.gather(
        _fetch_qingbao_page(schedule_id, sport),
        _fetch_live_page(schedule_id, sport),
        _fetch_trend_page(schedule_id, sport),
    )

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

    h2h = _parse_h2h(analysis_html, home_team, away_team)
    home_form = _parse_recent_form(analysis_html, "home", team_name=home_team, _tables=analysis_tables)
    away_form = _parse_recent_form(analysis_html, "away", team_name=away_team, _tables=analysis_tables)
    standings = _parse_standings(
        analysis_html,
        qingbao_html,
        _analysis_tables=analysis_tables,
        _qingbao_tables=qingbao_tables,
        home_team=home_team,
        away_team=away_team,
    )

    title = await _get_match_title(schedule_id, sport)
    if not _context_matches_declared_sport(
        sport=sport,
        title=title,
        h2h=h2h,
        home_form=home_form,
        away_form=away_form,
        standings=standings,
    ):
        logger.warning(
            "nowscore prefetch rejected by sport check: sport=%s sid=%s home=%s away=%s title=%s",
            sport, schedule_id, home_team, away_team, title,
        )
        return None

    # 解析额外维度
    analysis_extra = _parse_injuries_and_features(analysis_html)
    live_data = _parse_live_data(live_html)
    trend_data = _parse_odds_trend_tables(trend_html or analysis_html)

    ctx = {
        "source": "nowscore",
        "schedule_id": schedule_id,
        "sport": sport,
        "home_team": home_team,
        "away_team": away_team,
        "match_title": title,
        "analysis": {
            "injuries": analysis_extra.get("injuries") or [],
            "features": analysis_extra.get("features") or [],
            "compare": analysis_extra.get("compare") or [],
        },
        "live": live_data,
        "trend": trend_data,
        "h2h": h2h,
        "home_form": home_form,
        "away_form": away_form,
        "standings": standings,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    ctx = _sync_context_dimensions(ctx)
    if not (ctx.get("dimensions_present") or []):
        return None
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
                if not (ctx.get("dimensions_present") or []):
                    return

                # 计算 fixture_key 并保存
                from app.services.fixture_key import fixture_key
                from app.services.match_context_store import save_context

                resolved_home = str(ctx.get("home_team") or home or "").strip()
                resolved_away = str(ctx.get("away_team") or away or "").strip()
                if not resolved_home or not resolved_away:
                    return
                fk = fixture_key(sport, resolved_home, resolved_away)
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
