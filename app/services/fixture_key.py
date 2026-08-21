"""跨站同场（OB / 平博）去重：同场只分析一次、只下一单。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional, Sequence

from app.services.bookmakers.match_resolve import _norm_team, _pair_similarity

# 与同站模糊匹配阈值对齐，略放宽以覆盖中英/译名差异
_FIXTURE_SIM_THRESHOLD = 0.72
_START_WINDOW = timedelta(hours=6)


def fixture_key(
    sport: str,
    home_team: str,
    away_team: str,
    *,
    start_time: Optional[datetime] = None,
) -> str:
    """稳定缓存键：球类 + 规范化主客（按字典序避免主客对调分裂）。"""
    sport_n = (sport or "football").strip().lower()
    if sport_n == "soccer":
        sport_n = "football"
    a = _norm_team(home_team)
    b = _norm_team(away_team)
    pair = "|".join(sorted((a, b)))
    bucket = ""
    if start_time is not None:
        st = start_time
        if st.tzinfo is None:
            st = st.replace(tzinfo=timezone.utc)
        # 6 小时桶，避免同场开球时间微差拆成两键
        epoch = int(st.timestamp())
        bucket = f":{epoch // int(_START_WINDOW.total_seconds())}"
    return f"{sport_n}:{pair}{bucket}"


def same_fixture(
    a: Any,
    b: Any,
    *,
    threshold: float = _FIXTURE_SIM_THRESHOLD,
) -> bool:
    """两场是否视为同场（允许主客对调；开球时间差 ≤6h）。"""
    sport_a = getattr(a, "sport", None)
    sport_b = getattr(b, "sport", None)
    sa = sport_a.value if hasattr(sport_a, "value") else str(sport_a or "")
    sb = sport_b.value if hasattr(sport_b, "value") else str(sport_b or "")
    sa = "football" if sa == "soccer" else sa.lower()
    sb = "football" if sb == "soccer" else sb.lower()
    if sa != sb:
        return False

    sim = _pair_similarity(
        str(getattr(a, "home_team", "") or ""),
        str(getattr(a, "away_team", "") or ""),
        str(getattr(b, "home_team", "") or ""),
        str(getattr(b, "away_team", "") or ""),
    )
    if sim + 1e-9 < threshold:
        return False

    ta = getattr(a, "start_time", None)
    tb = getattr(b, "start_time", None)
    # start_time 是默认值（早于 2020 年）时跳过时间检查，只靠队名相似度匹配
    if ta and tb and ta.year > 2020 and tb.year > 2020:
        if ta.tzinfo is None:
            ta = ta.replace(tzinfo=timezone.utc)
        if tb.tzinfo is None:
            tb = tb.replace(tzinfo=timezone.utc)
        if abs((ta - tb).total_seconds()) > _START_WINDOW.total_seconds():
            return False
    return True


def group_matches_by_fixture(matches: Sequence[Any]) -> list[list[Any]]:
    """贪心聚类：同场一组（OB + 平博 等同场合并）。"""
    groups: list[list[Any]] = []
    for m in matches:
        placed = False
        for g in groups:
            if same_fixture(m, g[0]):
                g.append(m)
                placed = True
                break
        if not placed:
            groups.append([m])
    return groups


def pick_canonical_match(group: Sequence[Any]) -> Any:
    """选分析代表场：优先 OB，其次平博，再按 id。"""
    if not group:
        raise ValueError("empty fixture group")

    def _site(m: Any) -> str:
        ext = str(getattr(m, "external_id", "") or "")
        if ":" in ext:
            return ext.split(":", 1)[0].lower()
        extra = getattr(m, "extra_data", None) or {}
        src = str(extra.get("source") or "").lower()
        if src.startswith("ob"):
            return "ob"
        if "pinnacle" in src or src.startswith("pin"):
            return "pinnacle"
        return ""

    order = {"ob": 0, "pinnacle": 1}
    return sorted(
        group,
        key=lambda m: (order.get(_site(m), 9), int(getattr(m, "id", 0) or 0)),
    )[0]


def fixture_key_for_match(match: Any) -> str:
    sport = getattr(match, "sport", None)
    sport_s = sport.value if hasattr(sport, "value") else str(sport or "football")
    return fixture_key(
        sport_s,
        str(getattr(match, "home_team", "") or ""),
        str(getattr(match, "away_team", "") or ""),
        start_time=getattr(match, "start_time", None),
    )


async def sibling_match_ids(
    db,
    match: Any,
    *,
    statuses: Optional[Iterable[Any]] = None,
) -> list[int]:
    """查找同场的其他 Match.id（含自身）。"""
    from sqlalchemy import select

    from app.models.user import Match, MatchStatus

    sport = getattr(match, "sport", None)
    st_filter = list(statuses) if statuses is not None else [MatchStatus.LIVE, MatchStatus.UPCOMING]
    q = select(Match).where(Match.sport == sport, Match.status.in_(st_filter))
    if getattr(match, "start_time", None):
        st = match.start_time
        if st.tzinfo is not None:
            st = st.replace(tzinfo=None)  # 转为 naive，匹配 timestamp without time zone 列
        # start_time 是默认值（2000-01-01）时跳过时间窗口，避免平博赛事匹配不到
        if st.year > 2020:
            # 同时包含默认 start_time 的比赛（平博），same_fixture 会跳过它们的时间检查
            from sqlalchemy import or_
            default_cutoff = datetime(2001, 1, 1)  # naive datetime，匹配 timestamp without time zone
            q = q.where(
                or_(
                    (Match.start_time >= st - _START_WINDOW) & (Match.start_time <= st + _START_WINDOW),
                    Match.start_time <= default_cutoff,
                )
            )
    rows = list((await db.execute(q.limit(80))).scalars().all())
    ids = [int(match.id)]
    for m in rows:
        if int(m.id) == int(match.id):
            continue
        if same_fixture(match, m):
            ids.append(int(m.id))
    return ids
