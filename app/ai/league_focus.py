"""足球/篮球主流联赛识别与篮球比赛时长。"""
from __future__ import annotations

import re


_LOWER_TIER_MARKERS = (
    "乙级", "第二联赛", "丙级", "丁级", "戊级", "预备", "后备",
    "青年", "u17", "u18", "u19", "u20", "u21", "u23", "女子", "女足",
    "女篮", "友谊", "表演", "地区联赛", "发展联赛", "mls next",
)

_FOOTBALL_FOCUS = (
    # 五大联赛
    "英格兰超级联赛", "英超", "premier league",
    "西班牙甲级联赛", "西甲", "la liga",
    "意大利甲级联赛", "意甲", "serie a",
    "德国甲级联赛", "德甲", "bundesliga",
    "法国甲级联赛", "法甲", "ligue 1",
    # 用户指定的重点联赛
    "沙特职业联赛", "沙特超级联赛", "saudi pro league",
    "美国职业大联盟", "美职联", "major league soccer",
    "巴西甲级联赛", "巴甲", "brasileirao", "brasileirão",
    "葡萄牙超级联赛", "葡超", "primeira liga",
    "荷兰甲级联赛", "荷甲", "eredivisie",
    "阿根廷甲级联赛", "阿根廷超级联赛", "阿根廷职业联赛", "阿超", "liga profesional",
    "墨西哥超级联赛", "墨超", "liga mx",
    "欧洲冠军联赛", "欧冠", "uefa champions league",
)

_BASKETBALL_FOCUS = (
    "西班牙acb", "liga acb", "liga endesa",
    "欧洲篮球联赛", "欧洲篮球冠军联赛", "欧篮联", "euroleague",
)


def _norm(league: str) -> str:
    return re.sub(r"\s+", " ", str(league or "").strip().lower())


def _has(value: str, markers: tuple[str, ...]) -> bool:
    compact = re.sub(r"[\s_\-–—]+", "", value)
    return any(
        marker in value or re.sub(r"[\s_\-–—]+", "", marker) in compact
        for marker in markers
    )


def is_nba_league(league: str) -> bool:
    value = _norm(league)
    if "wnba" in value:
        return False
    return bool(re.search(r"(?:^|[^a-z])nba(?:[^a-z]|$)", value)) or "美国职业篮球联赛" in value


def league_focus_level(sport: str, league: str) -> int:
    """2=明确重点，1=合规超级/甲级联赛，0=普通联赛。"""
    value = _norm(league)
    sport_l = str(sport or "").strip().lower()
    if not value:
        return 0
    if _has(value, _LOWER_TIER_MARKERS):
        return 0
    if sport_l in ("football", "soccer"):
        if _has(value, _FOOTBALL_FOCUS):
            return 2
        if _has(value, ("超级联赛", "甲级联赛", "premier division", "first division")):
            return 1
        return 0
    if sport_l == "basketball":
        if is_nba_league(value) or _has(value, _BASKETBALL_FOCUS):
            return 2
        return 0
    return 0


def basketball_regulation_minutes(league: str) -> float:
    """NBA每节12分钟（48分钟）；ACB/欧篮联及其他FIBA赛事默认40分钟。"""
    value = _norm(league)
    # 缺联赛时沿用历史48分钟口径并让数据完整性闸门处理，避免静默猜测。
    if not value:
        return 48.0
    return 48.0 if is_nba_league(value) else 40.0


def basketball_quarter_minutes(league: str) -> float:
    return basketball_regulation_minutes(league) / 4.0


def league_priority_sort_key(match) -> tuple[int, str]:
    sport = getattr(match, "sport", "")
    if hasattr(sport, "value"):
        sport = sport.value
    league = getattr(match, "league", "")
    level = league_focus_level(str(sport), str(league))
    return (-level, _norm(str(league)))
