"""
进行中赛事判定：只认「已经开赛」的场次。

丢弃：未开赛却标 LIVE、开赛时间误当比分（如 5:30）、仅有墙钟无节次 等。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

_LIVE_STATUS = frozenset({"live", "inplay", "in_play", "running", "started"})
_FINISHED = frozenset({"finished", "ended", "closed", "settled", "完场"})

# 注意：单独「进行中/滚球」过弱（同步路径曾伪造 period=进行中），需配合比分/时钟
_INPLAY_PERIOD = re.compile(
    r"上半场|下半场|中场|加时|点球|"
    r"第[一二三四1-4]\s*节|第\s*[1-4]\s*节|"
    r"(?:^|[^a-z0-9])(?:Q[1-4]|1H|2H|HT|OT|ET|PEN|LIVE)(?:$|[^a-z0-9])",
    re.I,
)
_WEAK_INPLAY_PERIOD = re.compile(r"进行中|滚球", re.I)

_CLOCK_RE = re.compile(r"^(\d{1,3}):(\d{2})$")
_APOSTROPHE_CLOCK_RE = re.compile(r"^(\d{1,3})['′]$")
_KICKOFF_MINUTES = frozenset({0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55})


def _as_int(v: Any, default: int = 0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _parse_start(start_time: Any) -> Optional[datetime]:
    if start_time is None or start_time == "":
        return None
    if isinstance(start_time, datetime):
        return start_time if start_time.tzinfo else start_time.replace(tzinfo=timezone.utc)
    s = str(start_time).strip()
    if not s:
        return None
    if s.isdigit():
        try:
            n = int(s)
            if n > 10_000_000_000:
                n //= 1000
            return datetime.fromtimestamp(n, tz=timezone.utc)
        except Exception:
            return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def looks_like_kickoff_score(home_score: Any, away_score: Any) -> bool:
    """比分形如 5:30 / 10:00 —— 多为开球时刻误刮成比分（勿把 1-0/2-0 当真比分误杀）。"""
    hs = _as_int(home_score)
    aws = _as_int(away_score)
    if hs < 0 or aws < 0 or hs > 23:
        return False
    # 15/30/45 分的「比分」几乎必是开球时刻
    if aws in (15, 30, 45) and 0 <= hs <= 23:
        return True
    # 10:00–23:00；1:00–9:00 与常见足球比分 1-0…9-0 冲突，不按开球时刻杀
    if aws == 0 and 10 <= hs <= 23:
        return True
    if aws in _KICKOFF_MINUTES and aws not in (0, 15, 30, 45) and 0 <= hs <= 23:
        return True
    return False


def looks_like_wall_clock(clock: str) -> bool:
    """时钟像当天开球时刻（05:30 / 18:30 / 19:00）而非比赛进行分钟。

    平博滚球列表常把晚间开球墙钟（13–23:00/:30）刮成 clock；
    0–0 场景下必须与比赛分钟区分。
    """
    m = _CLOCK_RE.match((clock or "").strip())
    if not m:
        return False
    hh, mi = int(m.group(1)), int(m.group(2))
    if hh >= 24 or mi > 59:
        return False
    # 开球墙钟：任意小时 + :00 / :30（含 18:30/19:00/20:00）
    if mi in (0, 30):
        return True
    return False


def looks_like_match_minute_clock(clock: str) -> bool:
    """像比赛进行分钟（1–130 分，且不是典型开球墙钟）。"""
    c = (clock or "").strip()
    ap = _APOSTROPHE_CLOCK_RE.match(c)
    if ap:
        mm = int(ap.group(1))
        return 1 <= mm <= 130
    m = _CLOCK_RE.match(c)
    if not m:
        return False
    mm, ss = int(m.group(1)), int(m.group(2))
    if ss > 59 or mm < 1 or mm > 130:
        return False
    if looks_like_wall_clock(clock):
        return False
    return True


def has_inplay_period(period: str) -> bool:
    p = (period or "").strip()
    if not p or p in ("完场", "未开始", "即将开始"):
        return False
    return bool(_INPLAY_PERIOD.search(p))


def has_weak_inplay_period(period: str) -> bool:
    p = (period or "").strip()
    if not p or p in ("完场", "未开始", "即将开始"):
        return False
    return bool(_WEAK_INPLAY_PERIOD.search(p))


def is_actually_started(
    *,
    status: Any = "",
    period: str = "",
    clock: str = "",
    home_score: Any = 0,
    away_score: Any = 0,
    start_time: Any = None,
    now: Optional[datetime] = None,
) -> bool:
    """是否已开赛（可进「进行中」）。"""
    st = str(status or "").strip().lower()
    period = str(period or "").strip()
    clock = str(clock or "").strip()
    if st in _FINISHED or period == "完场":
        return False
    if st not in _LIVE_STATUS:
        return False

    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    start_dt = _parse_start(start_time)
    if start_dt is not None:
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)
        if start_dt > now + timedelta(minutes=2):
            return False

    hs = _as_int(home_score)
    aws = _as_int(away_score)

    if looks_like_kickoff_score(hs, aws) and not has_inplay_period(period):
        return False

    # 强证据：进行中节次（上半场 / 1H / Q1…）
    if has_inplay_period(period):
        return True

    # 有效比分（非假开球时刻）
    if (hs > 0 or aws > 0) and not looks_like_kickoff_score(hs, aws):
        return True

    # 「进行中/滚球」弱节次：0-0 时不信任 mm:ss（平博大量开球墙钟 01:45/19:35）
    if has_weak_inplay_period(period):
        if (hs > 0 or aws > 0) and not looks_like_kickoff_score(hs, aws):
            return True
        # 仅接受 67' 这类明确比赛分钟；不要用 01:45 这种墙钟
        if _APOSTROPHE_CLOCK_RE.match(clock.strip()):
            return looks_like_match_minute_clock(clock)
        return False

    # 无节次时：0-0 必须有明确比赛分钟（'）或非墙钟且不像开球点
    if hs == 0 and aws == 0:
        if not clock or looks_like_wall_clock(clock):
            return False
        if _APOSTROPHE_CLOCK_RE.match(clock.strip()):
            return looks_like_match_minute_clock(clock)
        return False

    return False


def remote_match_started(rm: Any, *, now: Optional[datetime] = None) -> bool:
    return is_actually_started(
        status=getattr(rm, "status", "") or "",
        period=str(getattr(rm, "period", "") or ""),
        clock=str(getattr(rm, "clock", "") or ""),
        home_score=getattr(rm, "home_score", 0) or 0,
        away_score=getattr(rm, "away_score", 0) or 0,
        start_time=getattr(rm, "start_time", None),
        now=now,
    )


def local_match_started(m: Any, *, now: Optional[datetime] = None) -> bool:
    extra = m.extra_data if isinstance(getattr(m, "extra_data", None), dict) else {}
    status = m.status.value if hasattr(getattr(m, "status", None), "value") else str(getattr(m, "status", "") or "")
    return is_actually_started(
        status=status,
        period=str(extra.get("period") or getattr(m, "period", "") or ""),
        clock=str(extra.get("clock") or getattr(m, "clock", "") or ""),
        home_score=getattr(m, "home_score", 0) or 0,
        away_score=getattr(m, "away_score", 0) or 0,
        start_time=getattr(m, "start_time", None),
        now=now,
    )


def parse_match_clock_minutes(clock: str, *, allow_countdown: bool = False) -> Optional[float]:
    """解析比赛时钟为分钟数。支持 67' / 67:12。

    allow_countdown=True：篮球节内倒计时（含 8:30 / 3:00），不按开球墙钟剔除。
    """
    c = (clock or "").strip()
    if not c:
        return None
    ap = _APOSTROPHE_CLOCK_RE.match(c)
    if ap:
        mm = float(ap.group(1))
        return mm if 0 <= mm <= 130 else None
    m = _CLOCK_RE.match(c)
    if not m:
        return None
    mm, ss = int(m.group(1)), int(m.group(2))
    if ss > 59 or mm < 0 or mm > 130:
        return None
    if not allow_countdown and looks_like_wall_clock(c):
        return None
    return mm + ss / 60.0


def match_elapsed_seconds(
    *,
    sport: str = "",
    period: str = "",
    clock: str = "",
) -> Optional[int]:
    """估算已进行秒数（越小越「刚开赛」）。无法解析返回 None。"""
    sport_l = (sport or "").lower().strip()
    period_l = (period or "").strip()
    mins = parse_match_clock_minutes(clock)
    if mins is None:
        # 无时钟：用节次粗分档，便于排序（刚开赛优先）
        pl = period_l.lower()
        if any(x in pl for x in ("完场", "finished", "ft")):
            return 10**9
        if any(x in period_l for x in ("点球", "PEN")):
            return 120 * 60
        if any(x in period_l for x in ("加时", "OT", "ET")):
            return 95 * 60
        if any(x in period_l for x in ("下半场", "2H", "第4节", "Q4")):
            return 70 * 60
        if any(x in period_l for x in ("中场", "HT", "节间")):
            return 45 * 60
        if any(x in period_l for x in ("上半场", "1H", "第1节", "Q1")):
            return 15 * 60
        if any(x in period_l for x in ("第2节", "Q2")):
            return 30 * 60
        if any(x in period_l for x in ("第3节", "Q3")):
            return 45 * 60
        return None

    secs = int(mins * 60)
    # 足球：OB 多为累计分钟；中场/加时修正
    if sport_l in ("football", "soccer", ""):
        if "中场" in period_l or re.search(r"\bHT\b", period_l, re.I):
            return max(secs, 45 * 60)
        if "加时上" in period_l:
            return 90 * 60 + secs
        if "加时下" in period_l:
            return 105 * 60 + secs
        if "加时" in period_l or re.search(r"\b(?:OT|ET)\b", period_l, re.I):
            return 90 * 60 + secs
        return secs

    # 篮球：时钟多为节内倒计时，用节次估算已进行
    if sport_l == "basketball":
        pl = period_l.lower()
        mins_cd = parse_match_clock_minutes(clock, allow_countdown=True)
        if mins_cd is None:
            mins_cd = mins
        if mins_cd is None:
            return None
        # Q4/加时倒计时：已进行 ≈ 节长 - 剩余；按 12 分钟一节粗算
        quarter_len = 12.0
        if any(x in period_l for x in ("第4节", "Q4")) or re.search(r"\bq4\b", pl):
            played_in_q = max(0.0, quarter_len - mins_cd)
            return int((36 + played_in_q) * 60)
        if any(x in period_l for x in ("加时", "OT")) or re.search(r"\bot\b", pl):
            played_in_ot = max(0.0, 5.0 - mins_cd)
            return int((48 + played_in_ot) * 60)
        if any(x in period_l for x in ("第3节", "Q3")) or re.search(r"\bq3\b", pl):
            return int((24 + max(0.0, quarter_len - mins_cd)) * 60)
        if any(x in period_l for x in ("第2节", "Q2")) or re.search(r"\bq2\b", pl):
            return int((12 + max(0.0, quarter_len - mins_cd)) * 60)
        if any(x in period_l for x in ("第1节", "Q1")) or re.search(r"\bq1\b", pl):
            return int(max(0.0, quarter_len - mins_cd) * 60)
        return int(mins_cd * 60)

    return secs


def estimate_remaining_minutes(
    *,
    sport: str,
    period: str = "",
    clock: str = "",
) -> Optional[float]:
    """估算距离常规结束还剩多少分钟；无法判断返回 None。"""
    sport_l = (sport or "").lower().strip()
    period_l = (period or "").strip()
    pl = period_l.lower()
    mins = parse_match_clock_minutes(clock)

    if any(x in period_l for x in ("完场", "点球")) or re.search(r"\b(?:FT|PEN)\b", period_l, re.I):
        return 0.0

    if sport_l in ("football", "soccer"):
        if "中场" in period_l or re.search(r"\bHT\b", period_l, re.I):
            return 45.0
        if any(x in period_l for x in ("上半场", "1H")) or re.search(r"\b1H\b", period_l, re.I):
            if mins is None:
                return 30.0  # 保守：上半场默认未到尾声
            # 累计或节内分钟：上半场剩余
            elapsed = min(max(mins, 0.0), 45.0)
            return max(0.0, 45.0 - elapsed) + 45.0  # 含下半场
        if any(x in period_l for x in ("下半场", "2H")) or re.search(r"\b2H\b", period_l, re.I):
            if mins is None:
                return None
            # 累计分钟常见 45–90+；若 <45 视为节内分钟
            elapsed = mins if mins >= 40 else (45.0 + mins)
            return max(0.0, 90.0 - elapsed)
        if any(x in period_l for x in ("加时", "OT", "ET")) or re.search(r"\b(?:OT|ET)\b", period_l, re.I):
            if mins is None:
                return 5.0
            # 加时剩余很短
            return max(0.0, 15.0 - min(mins, 15.0))
        # 无节次：仅当时钟已很高才判将结束
        if mins is not None and mins >= 80:
            return max(0.0, 90.0 - mins)
        return None

    if sport_l == "basketball":
        mins_cd = parse_match_clock_minutes(clock, allow_countdown=True)
        # Q1–Q3：不会在 10 分钟内结束整场
        if any(x in period_l for x in ("第1节", "第2节", "第3节", "Q1", "Q2", "Q3")) or re.search(
            r"\bq[123]\b", pl
        ):
            return 20.0
        if "节间" in period_l or "中场" in period_l or "休息" in period_l:
            return 15.0
        if any(x in period_l for x in ("第4节", "Q4")) or re.search(r"\bq4\b", pl):
            if mins_cd is None:
                return None
            return max(0.0, mins_cd)
        if any(x in period_l for x in ("加时", "OT")) or re.search(r"\bot\b", pl):
            if mins_cd is None:
                return 2.0
            return max(0.0, mins_cd)
        return None

    return None


def is_ending_within_minutes(
    *,
    sport: str,
    period: str = "",
    clock: str = "",
    minutes: float = 10.0,
) -> bool:
    """是否预计在 N 分钟内结束（不分析 / 不展示）。"""
    rem = estimate_remaining_minutes(sport=sport, period=period, clock=clock)
    if rem is None:
        return False
    return rem <= float(minutes) + 1e-9


def total_goals_exceed_line(
    home_score: Any,
    away_score: Any,
    total_line: Any,
) -> bool:
    """当前总比分是否已超过大小球盘口（大球已成 / 小球已死）。"""
    if total_line is None or total_line == "":
        return False
    try:
        line = float(total_line)
    except (TypeError, ValueError):
        return False
    if line <= 0:
        return False
    hs = _as_int(home_score)
    aws = _as_int(away_score)
    if looks_like_kickoff_score(hs, aws):
        return False
    goals = hs + aws
    return float(goals) > line + 1e-9


def match_analysis_skip_reason(
    *,
    sport: str,
    period: str = "",
    clock: str = "",
    home_score: Any = 0,
    away_score: Any = 0,
    total_line: Any = None,
    ending_minutes: float = 10.0,
) -> Optional[str]:
    """返回跳过分析原因；可分析则 None。"""
    if total_goals_exceed_line(home_score, away_score, total_line):
        return "score_over_line"
    if is_ending_within_minutes(
        sport=sport, period=period, clock=clock, minutes=ending_minutes
    ):
        return "ending_soon"
    return None


def local_match_clock_period(m: Any) -> tuple[str, str, str]:
    """(sport, period, clock) from DB Match."""
    sport = m.sport.value if hasattr(getattr(m, "sport", None), "value") else str(getattr(m, "sport", "") or "")
    extra = m.extra_data if isinstance(getattr(m, "extra_data", None), dict) else {}
    period = str(extra.get("period") or getattr(m, "period", "") or "")
    clock = str(extra.get("clock") or getattr(m, "clock", "") or "")
    return sport, period, clock
