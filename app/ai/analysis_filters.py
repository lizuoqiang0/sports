"""AI 分析候选过滤：超盘口 / 即将结束 / 赔率区间；刚开赛优先。

赔率下限/上限严格跟随用户 AI 配置「赔率范围」(min_odds / max_odds)，
人工推荐与自动引擎共用同一套参数。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from app.services.bookmakers.match_live import (
    is_ending_within_minutes,
    local_match_clock_period,
    match_analysis_skip_reason,
    match_elapsed_seconds,
    total_goals_exceed_line,
)

logger = logging.getLogger(__name__)

ENDING_SOON_MINUTES = 10.0
# 仅当配置缺失时的兜底（正常路径必须传入 AIConfig 赔率区间）
DEFAULT_MIN_ODDS = 1.1
DEFAULT_MAX_ODDS = 10.0


def _as_eu_odds(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if x <= 1.01 or x > 500:
        return None
    return x


def usable_total_odds(odds_map: Any) -> list[float]:
    if not isinstance(odds_map, dict):
        return []
    out: list[float] = []
    # 大小球两边都可成为 AI 候选；只看 under 会把 over-only 的平博/OB
    # 盘口在扫描阶段误判为“无可用赔率”，从而根本进不了分析与闸门。
    for selection in ("under", "over"):
        x = _as_eu_odds(odds_map.get(selection))
        if x is not None:
            out.append(x)
    return out


def odds_in_configured_range(
    odds: Any,
    *,
    min_odds: float = DEFAULT_MIN_ODDS,
    max_odds: float | None = DEFAULT_MAX_ODDS,
) -> bool:
    x = _as_eu_odds(odds)
    if x is None:
        return False
    lo = float(min_odds)
    if x + 1e-9 < lo:
        return False
    if max_odds is not None and x - 1e-9 > float(max_odds):
        return False
    return True


def total_odds_meet_min(
    odds_map: Any,
    *,
    floor: float = DEFAULT_MIN_ODDS,
    ceiling: float | None = DEFAULT_MAX_ODDS,
) -> bool:
    """全场大小球任一方向赔率落在配置区间内才值得分析。"""
    return any(
        odds_in_configured_range(v, min_odds=floor, max_odds=ceiling)
        for v in usable_total_odds(odds_map)
    )


def any_market_odds_meet_min(
    odds_map: Any,
    *,
    floor: float = DEFAULT_MIN_ODDS,
    ceiling: float | None = DEFAULT_MAX_ODDS,
) -> bool:
    """胜负/让球/大小任一盘口有区间内赔率即可分析。"""
    if not isinstance(odds_map, dict):
        return total_odds_meet_min(odds_map, floor=floor, ceiling=ceiling)
    markets = odds_map.get("markets") if isinstance(odds_map.get("markets"), dict) else None
    if markets:
        for entry in markets.values():
            odds = entry.get("odds") if isinstance(entry, dict) else entry
            if not isinstance(odds, dict):
                continue
            for v in odds.values():
                if odds_in_configured_range(v, min_odds=floor, max_odds=ceiling):
                    return True
        return False
    # 扁平：home/away/draw/under
    vals = []
    for k, v in odds_map.items():
        if str(k).startswith("_") or k in ("markets", "line", "total", "spread"):
            continue
        x = _as_eu_odds(v)
        if x is not None:
            vals.append(x)
    if not vals:
        return total_odds_meet_min(odds_map, floor=floor, ceiling=ceiling)
    return any(odds_in_configured_range(v, min_odds=floor, max_odds=ceiling) for v in vals)


def skip_reason_for_match(
    m: Any,
    total_line: Any = None,
    *,
    odds_map: Any = None,
    min_odds: float = DEFAULT_MIN_ODDS,
    max_odds: float | None = DEFAULT_MAX_ODDS,
    require_total_only: bool = False,
) -> Optional[str]:
    from app.services.bookmakers.china_match import is_china_match

    league = getattr(m, "league", None) or (m.get("league") if isinstance(m, dict) else "") or ""
    home = getattr(m, "home_team", None) or (m.get("home_team") if isinstance(m, dict) else "") or ""
    away = getattr(m, "away_team", None) or (m.get("away_team") if isinstance(m, dict) else "") or ""
    sport = getattr(m, "sport", None) or (m.get("sport") if isinstance(m, dict) else "") or ""
    if hasattr(sport, "value"):
        sport = sport.value
    if is_china_match(str(league), str(home), str(away), str(sport)):
        return "china_match"

    # 联赛黑名单前置过滤（青少年/女子赛事）：扫描层直接跳过，省一次 LLM 调用
    from app.ai.strategy import league_is_blacklisted

    if league_is_blacklisted(str(league)):
        return "league_blacklisted"

    sport_l = str(sport or "").lower()
    # 只分析有全场大小球赔率(TOTAL under/over)的比赛。
    # 足球和篮球都强制 require_total_only：无 TOTAL 赔率的比赛直接跳过
    check = total_odds_meet_min
    if odds_map is not None and not check(odds_map, floor=min_odds, ceiling=max_odds):
        return "odds_out_of_range"
    sport_key, period, clock = local_match_clock_period(m)
    # 足球有胜负/让球时，比分超大小线不再整场跳过
    line_for_skip = total_line
    if sport_l in ("football", "soccer") and not require_total_only:
        markets = None
        if isinstance(odds_map, dict):
            markets = odds_map.get("markets") if isinstance(odds_map.get("markets"), dict) else None
        if markets and (markets.get("moneyline") or markets.get("spread")):
            line_for_skip = None
    return match_analysis_skip_reason(
        sport=sport_key,
        period=period,
        clock=clock,
        home_score=getattr(m, "home_score", 0) or 0,
        away_score=getattr(m, "away_score", 0) or 0,
        total_line=line_for_skip,
        ending_minutes=ENDING_SOON_MINUTES,
    )


def elapsed_sort_key(m: Any) -> tuple:
    """刚开赛优先：已进行秒数升序；同秒按开球时间升序。"""
    sport, period, clock = local_match_clock_period(m)
    elapsed = match_elapsed_seconds(sport=sport, period=period, clock=clock)
    el = int(elapsed) if elapsed is not None else 10**8
    st = getattr(m, "start_time", None)
    st_ts = st.timestamp() if st is not None and hasattr(st, "timestamp") else 0.0
    return (el, st_ts, int(getattr(m, "id", 0) or 0))


def sort_just_started_first(matches: list[Any]) -> list[Any]:
    return sorted(matches or [], key=elapsed_sort_key)


def _rec_primary_odds(rec: dict) -> Optional[float]:
    r = rec.get("recommendation") or {}
    for src in (
        r.get("odds"),
        (r.get("single") or {}).get("odds") if isinstance(r.get("single"), dict) else None,
    ):
        x = _as_eu_odds(src)
        if x is not None:
            return x
    for mkt in rec.get("markets") or []:
        if not isinstance(mkt, dict):
            continue
        single = mkt.get("single") or {}
        if isinstance(single, dict):
            x = _as_eu_odds(single.get("odds"))
            if x is not None:
                return x
    cur = rec.get("current_odds") or {}
    if isinstance(cur, dict):
        sel = str(r.get("selection") or "").lower()
        if sel in cur:
            return _as_eu_odds(cur.get(sel))
        vals = usable_total_odds(cur)
        if vals:
            return max(vals)
    return None


def rec_skip_reason(
    rec: dict,
    *,
    min_odds: float = DEFAULT_MIN_ODDS,
    max_odds: float | None = DEFAULT_MAX_ODDS,
) -> Optional[str]:
    """展示层：中国赛事 / 即将结束 / 比分已超盘 / 赔率不在配置区间 → 不显示。"""
    if not isinstance(rec, dict):
        return "invalid"
    from app.services.bookmakers.china_match import is_china_match

    if is_china_match(
        str(rec.get("league") or ""),
        str(rec.get("home_team") or ""),
        str(rec.get("away_team") or ""),
        str(rec.get("sport") or ""),
    ):
        return "china_match"
    sport = str(rec.get("sport") or "")
    period = str(rec.get("period") or "")
    clock = str(rec.get("clock") or "")
    hs = rec.get("home_score")
    aws = rec.get("away_score")
    line = rec.get("total_line")
    if line is None:
        line = (rec.get("recommendation") or {}).get("line")
    if total_goals_exceed_line(hs, aws, line):
        return "score_exceeds_line"
    if is_ending_within_minutes(
        sport=sport, period=period, clock=clock, minutes=ENDING_SOON_MINUTES
    ):
        return "ending_soon"
    primary = _rec_primary_odds(rec)
    if primary is not None and not odds_in_configured_range(
        primary, min_odds=min_odds, max_odds=max_odds
    ):
        return "odds_out_of_range"
    cur = rec.get("current_odds")
    if isinstance(cur, dict) and cur and not total_odds_meet_min(
        cur, floor=min_odds, ceiling=max_odds
    ):
        return "odds_out_of_range"
    return None


async def enrich_recs_skip_from_db(
    recommendations: list[dict],
    *,
    min_odds: float = DEFAULT_MIN_ODDS,
    max_odds: float | None = DEFAULT_MAX_ODDS,
) -> list[dict]:
    """用库内最新比分/时钟补全推荐，并按配置赔率区间过滤。"""
    from sqlalchemy import select

    from app.database import AsyncSessionLocal
    from app.models.user import BetType, Match, Odds

    recs = [r for r in (recommendations or []) if isinstance(r, dict)]
    ids = [int(r["match_id"]) for r in recs if r.get("match_id") is not None]
    if not ids:
        return [
            r
            for r in recs
            if not rec_skip_reason(r, min_odds=min_odds, max_odds=max_odds)
        ]

    async with AsyncSessionLocal() as db:
        rows = list((await db.execute(select(Match).where(Match.id.in_(ids)))).scalars().all())
        by_id = {int(m.id): m for m in rows}
        odds_rows = list(
            (
                await db.execute(
                    select(Odds.match_id, Odds.total, Odds.odds_data).where(
                        Odds.match_id.in_(ids),
                        Odds.bet_type == BetType.TOTAL,
                        Odds.valid_to.is_(None),
                    )
                )
            ).all()
        )
        line_by: dict[int, float] = {}
        odds_by: dict[int, dict] = {}
        for mid, total, odata in odds_rows:
            mid_i = int(mid)
            if isinstance(odata, dict):
                prev = odds_by.get(mid_i) or {}
                merged = dict(prev)
                for k in ("under", "over"):
                    if odata.get(k) is not None:
                        merged[k] = odata.get(k)
                odds_by[mid_i] = merged
            if total is None:
                continue
            try:
                line_by[mid_i] = float(total)
            except (TypeError, ValueError):
                continue
        # 只读查询，无需 commit

    out: list[dict] = []
    for r in recs:
        mid = r.get("match_id")
        m = by_id.get(int(mid)) if mid is not None else None
        if m is not None:
            sport, period, clock = local_match_clock_period(m)
            r = dict(r)
            r["sport"] = sport or r.get("sport")
            r["period"] = period or r.get("period")
            r["clock"] = clock or r.get("clock")
            r["home_score"] = getattr(m, "home_score", r.get("home_score"))
            r["away_score"] = getattr(m, "away_score", r.get("away_score"))
            if int(m.id) in line_by:
                r["total_line"] = line_by[int(m.id)]
            omap = odds_by.get(int(m.id)) or r.get("current_odds")
            if omap:
                r["current_odds"] = omap
            why = skip_reason_for_match(
                m,
                r.get("total_line"),
                odds_map=omap,
                min_odds=min_odds,
                max_odds=max_odds,
            )
            if why:
                continue
            if rec_skip_reason(r, min_odds=min_odds, max_odds=max_odds):
                continue
        elif rec_skip_reason(r, min_odds=min_odds, max_odds=max_odds):
            continue
        out.append(r)
    return out
