"""下单时赔率变动策略：≥1.7 自动接受，<1.7 放弃。"""
from __future__ import annotations

from typing import Optional

# 赔率变动后可接受的最低亚洲盘
ODDS_CHANGE_ACCEPT_FLOOR = 1.7


def _as_odds(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if x <= 1.0 or x > 500:
        return None
    return x


def odds_meaningfully_changed(original: float | None, current: float | None, *, eps: float = 0.005) -> bool:
    o = _as_odds(original)
    c = _as_odds(current)
    if o is None or c is None:
        return False
    return abs(c - o) > eps


def decide_odds_change(
    original: float | None,
    current: float | None,
    *,
    floor: float = ODDS_CHANGE_ACCEPT_FLOOR,
) -> tuple[bool, str, Optional[float]]:
    """
    返回 (是否继续下单, 原因, 应用赔率)。

    - 未变动：继续，用当前/原赔率
    - 变动且当前 ≥ floor：继续并接受新赔率
    - 变动且当前 < floor：放弃
    """
    o = _as_odds(original)
    c = _as_odds(current)
    if c is None and o is None:
        return False, "无有效赔率", None
    if c is None:
        return True, "无最新赔率，沿用报价", o
    if o is None:
        if c + 1e-9 >= float(floor):
            return True, f"采用最新赔率 {c}", c
        return False, f"最新赔率 {c} < {floor}，放弃下单", c
    if not odds_meaningfully_changed(o, c):
        return True, "赔率未变", c
    if c + 1e-9 >= float(floor):
        return True, f"赔率变动 {o}->{c} ≥ {floor}，自动接受", c
    return False, f"赔率变动 {o}->{c} < {floor}，放弃下单", c
