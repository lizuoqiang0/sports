"""
足球 / 篮球严格分类：禁止默认足球，禁止跨球类匹配。

优先级：
1. 显式 sportId / csid / 枚举文案
2. 强关键词（篮球 / 足球）
3. 节次（第N节 / Q1–Q4 → 篮球）
4. 比分启发式（高分 → 篮球）
5. 页面 Tab 提示（仅作弱证据）
无法判定 → None（丢弃，绝不默认 football）
"""
from __future__ import annotations

import re
from typing import Any, Optional

SUPPORTED_SPORTS = frozenset({"football", "basketball"})

# OB csid 及常见站内数字/字符串 ID
_SPORT_ID_MAP: dict[str, str] = {
    "1": "football",
    "2": "basketball",
    "football": "football",
    "soccer": "football",
    "basketball": "basketball",
    "足球": "football",
    "篮球": "basketball",
    "ft": "football",
    "bk": "basketball",
    "bb": "basketball",
}

_BB_STRONG = re.compile(
    r"篮|nba|cba|wnba|nbl|basket|euroleague|b\.?\s*league|ncaa\s*篮|"
    r"篮球联赛|男篮|女篮|季后赛.*篮",
    re.I,
)
_FB_STRONG = re.compile(
    r"soccer|fifa|英超|西甲|意甲|德甲|法甲|中超|欧冠|欧联|世预赛|世界杯|"
    r"亚冠|足总杯|联合会杯|足球|football|(?<![篮电])足(?!球联赛)",
    re.I,
)
# 「联赛/杯/超/甲」单独出现不能定足球（篮球联赛也有）
_FB_WEAK = re.compile(
    r"英超|西甲|意甲|德甲|法甲|中超|欧冠|足球|"
    r"乙级|甲级|超联|超级联赛|足球联赛|足协|世俱杯|亚洲杯|欧洲杯|"
    r"Premier\s*League|La\s*Liga|Serie\s*A|Bundesliga|Ligue\s*1|"
    r"Chilean|Primera|Championship",
    re.I,
)

_BB_PERIOD = re.compile(
    r"(?:^|[^a-z0-9])(?:Q[1-4]|第[一二三四1-4]节|第\s*[1-4]\s*节)(?:$|[^a-z0-9])|"
    r"加时\s*第|OT\s*\d|半场结束.*篮",
    re.I,
)
_FB_PERIOD = re.compile(r"上半场|下半场|中场|加时上|加时下|(?:^|[^a-z])(?:1H|2H|HT|ET)(?:$|[^a-z])", re.I)

# 非足球/篮球项目：即使页面 Tab 误标球类也必须丢弃
_UNSUPPORTED_EVENT = re.compile(
    r"一级方程式|方程式赛车|\bf1\b|formula\s*1|moto\s*gp|motogp|"
    r"网球|tennis|乒乓球|table\s*tennis|羽毛球|badminton|"
    r"排球|volleyball|棒球|baseball|冰球|hockey|美式足球|nfl|"
    r"手球|handball|高尔夫|golf|拳击|boxing|mma|ufc|"
    r"斯诺克|snooker|赛车|rally|赛车运动|田径|游泳|电竞|"
    r"板球|cricket|橄榄球|rugby|飞镖|darts",
    re.I,
)


def is_unsupported_event_text(text: str = "") -> bool:
    """联赛/队名含明确非足球篮球项目时返回 True。"""
    return bool(text and _UNSUPPORTED_EVENT.search(text))


def normalize_sport(value: Any) -> Optional[str]:
    """归一化为 football|basketball；无法识别返回 None。"""
    if value is None:
        return None
    if hasattr(value, "value"):
        value = value.value
    raw = str(value or "").strip().lower()
    if not raw:
        return None
    if raw in ("soccer",):
        return "football"
    if raw in SUPPORTED_SPORTS:
        return raw
    mapped = _SPORT_ID_MAP.get(raw) or _SPORT_ID_MAP.get(str(value).strip())
    if mapped in SUPPORTED_SPORTS:
        return mapped
    return None


def sport_from_id(sport_id: Any) -> Optional[str]:
    if sport_id is None or sport_id == "":
        return None
    key = str(sport_id).strip()
    mapped = _SPORT_ID_MAP.get(key) or _SPORT_ID_MAP.get(key.lower())
    return mapped if mapped in SUPPORTED_SPORTS else None


def looks_like_basketball_score(home_score: Any = 0, away_score: Any = 0) -> bool:
    try:
        hs = int(home_score or 0)
        aws = int(away_score or 0)
    except (TypeError, ValueError):
        return False
    if hs < 0 or aws < 0:
        return False
    # 单队 ≥20 或总分 ≥40：足球几乎不可能
    if max(hs, aws) >= 20 or (hs + aws) >= 40:
        return True
    # 单队 ≥15 且双方都已得分：偏篮球（足球少见）
    if max(hs, aws) >= 15 and min(hs, aws) >= 5:
        return True
    return False


def looks_like_football_score(home_score: Any = 0, away_score: Any = 0) -> bool:
    try:
        hs = int(home_score or 0)
        aws = int(away_score or 0)
    except (TypeError, ValueError):
        return False
    if hs < 0 or aws < 0:
        return False
    # 双方都 ≤10 且总分 ≤12：更像足球；不能单独据此定足球
    return max(hs, aws) <= 10 and (hs + aws) <= 12


def has_basketball_period(period: str = "", text: str = "") -> bool:
    return bool(_BB_PERIOD.search(period or "") or _BB_PERIOD.search(text or ""))


def is_credible_live_basketball(
    *,
    period: str = "",
    clock: str = "",
    home_score: Any = 0,
    away_score: Any = 0,
    text: str = "",
) -> bool:
    """真实篮球滚球：必须有 Q节/第N节，或可信高分。拒 1-0+01:00 伪篮球。"""
    if has_basketball_period(period, text):
        return True
    if looks_like_basketball_score(home_score, away_score):
        return True
    return False


def sports_compatible(a: Any, b: Any) -> bool:
    na = normalize_sport(a)
    nb = normalize_sport(b)
    return bool(na and nb and na == nb)


def sport_label_zh(sport: Any) -> str:
    n = normalize_sport(sport)
    if n == "football":
        return "足球"
    if n == "basketball":
        return "篮球"
    return "未知球类"


def classify_sport(
    *,
    sport_id: Any = None,
    sport_field: Any = None,
    text: str = "",
    period: str = "",
    home_score: Any = 0,
    away_score: Any = 0,
    sport_hint: Any = None,
) -> Optional[str]:
    """
    严格判定球类。冲突时：强关键词 > ID > 节次/比分 > hint。
    无法唯一确定 → None。
    """
    blob = f"{text or ''} {period or ''}"
    if is_unsupported_event_text(blob):
        return None
    from_id = sport_from_id(sport_id)
    from_field = normalize_sport(sport_field)
    bb_kw = bool(_BB_STRONG.search(blob))
    fb_kw = bool(_FB_STRONG.search(blob))
    # 弱足球词：仅当没有篮球证据时使用
    if not fb_kw and _FB_WEAK.search(blob) and not bb_kw:
        fb_kw = True

    bb_period = bool(_BB_PERIOD.search(period or "") or _BB_PERIOD.search(blob))
    fb_period = bool(_FB_PERIOD.search(period or ""))
    bb_score = looks_like_basketball_score(home_score, away_score)

    # 显式冲突：篮球关键词 vs 足球关键词
    if bb_kw and fb_kw:
        # 比分/节次打破平局
        if bb_period or bb_score:
            return "basketball"
        return None

    if bb_kw:
        return "basketball"
    if fb_kw and not bb_score and not bb_period:
        return "football"
    if fb_kw and (bb_score or bb_period):
        # 文案像足球但比分/节次像篮球 → 丢弃，避免错标
        return None

    if from_id and from_field and from_id != from_field:
        return None
    if from_id:
        # ID 与比分冲突时丢弃
        if from_id == "football" and (bb_score or bb_period):
            return None
        if from_id == "basketball" and fb_period and looks_like_football_score(home_score, away_score) and not bb_score:
            # 罕见：篮球 ID 但半场+低分——仍信 ID
            return "basketball"
        return from_id
    if from_field:
        if from_field == "football" and (bb_score or bb_period):
            return None
        return from_field

    if bb_period or bb_score:
        return "basketball"

    # 上/下半场 + 低比分 + 无篮球证据 → 足球
    if fb_period and looks_like_football_score(home_score, away_score) and not bb_kw:
        return "football"

    hint = normalize_sport(sport_hint)
    # 篮球 hint  alone 不够：必须有节次或高分，否则易把早盘/误刮低分标成篮球
    if hint == "basketball" and (bb_period or bb_score):
        return "basketball"
    if hint == "football" and not bb_score and not bb_period:
        return "football"

    # 绝无默认足球
    return None


def reject_sport_mismatch(
    declared: Any,
    *,
    period: str = "",
    home_score: Any = 0,
    away_score: Any = 0,
    text: str = "",
) -> bool:
    """已声明球类与比分/节次明显矛盾时返回 True（应丢弃）。"""
    if is_unsupported_event_text(f"{text or ''} {period or ''}"):
        return True
    sport = normalize_sport(declared)
    if not sport:
        return True
    if sport == "football" and (
        looks_like_basketball_score(home_score, away_score)
        or _BB_PERIOD.search(period or "")
        or _BB_STRONG.search(text or "")
    ):
        return True
    if sport == "basketball":
        # 伪篮球：低分 + 无 Q/第N节（如 1-0 / 8-0 + 01:00）
        if not is_credible_live_basketball(
            period=period,
            home_score=home_score,
            away_score=away_score,
            text=text,
        ):
            return True
        if _FB_STRONG.search(text or "") and looks_like_football_score(home_score, away_score):
            return True
    return False
