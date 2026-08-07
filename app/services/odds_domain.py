"""Pure, auditable domain helpers for odds monitoring.

These functions deliberately have no database or network dependency so they can
be reused by adapters, scanners and tests without accidentally placing orders.

平台约定：分析 / 下单一律使用亚洲盘（小数赔率 decimal / EU）。
"""
from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Any, Iterable, Mapping, Optional

# 亚洲盘 = 小数赔率（EU / Decimal）。全站统一口径。
EUROPEAN_ODDS_FORMAT = "decimal"
EUROPEAN_ODDS_ALIASES = frozenset({"decimal", "european", "eu", "euro", "亚盘", "亚洲盘", "亚赔"})


def to_decimal(raw: Decimal | int | float | str, fmt: str) -> Decimal:
    value = Decimal(str(raw).replace(",", "").strip())
    if value == 0:
        raise ValueError("odds must be positive")
    fmt = (fmt or EUROPEAN_ODDS_FORMAT).lower().strip()
    if fmt in EUROPEAN_ODDS_ALIASES:
        result = value
    elif fmt in {"hong_kong", "hk", "香港盘", "港盘"}:
        if value < 0:
            raise ValueError("hong_kong odds cannot be negative")
        result = Decimal(1) + value
    elif fmt in {"malay", "indonesian", "马来盘", "印尼盘"}:
        result = Decimal(1) + value if value >= 0 else Decimal(1) / abs(value)
    elif fmt in {"american", "us", "美式盘", "美盘"}:
        result = Decimal(1) + value / Decimal(100) if value > 0 else Decimal(1) + Decimal(100) / abs(value)
    else:
        raise ValueError(f"unsupported odds format: {fmt}")
    if result <= 1:
        raise ValueError("decimal odds must be greater than 1")
    return result


def detect_odds_format(raw: Decimal | int | float | str) -> str:
    """粗判原始赔率格式。亚洲盘优先；仅对无歧义区间自动识别。"""
    value = Decimal(str(raw).replace(",", "").strip())
    if Decimal("-1") < value < 0:
        return "malay"
    if 0 < value < 1:
        # 亚洲盘恒 >1；该区间只能是港盘/马来正数（换算公式同港盘正数）
        return "hong_kong"
    if abs(value) >= 100:
        return "american"
    return EUROPEAN_ODDS_FORMAT


def coerce_to_european(
    raw: Any,
    fmt: str | None = None,
    *,
    min_odds: Decimal = Decimal("1.01"),
    max_odds: Decimal = Decimal("500"),
) -> Optional[Decimal]:
    """把任意站点原始赔率规范为亚洲盘小数；非法则返回 None。"""
    if raw is None or raw == "":
        return None
    try:
        text = str(raw).replace(",", "").strip()
        if not text or text.lower() in ("null", "none", "undefined", "-", "—"):
            return None
        use_fmt = (fmt or "").strip() or detect_odds_format(text)
        if use_fmt in EUROPEAN_ODDS_ALIASES:
            use_fmt = EUROPEAN_ODDS_FORMAT
        result = to_decimal(text, use_fmt)
    except (TypeError, ValueError, ArithmeticError):
        return None
    if result < min_odds or result > max_odds:
        return None
    return result.quantize(Decimal("0.001"))


def coerce_float_european(raw: Any, fmt: str | None = None) -> Optional[float]:
    d = coerce_to_european(raw, fmt)
    return float(d) if d is not None else None


def normalize_odds_data_to_european(odds_data: Mapping | None) -> dict:
    """规范化 odds_data 公开字段为亚洲盘；保留 _ob/_site 等内部引用。"""
    if not isinstance(odds_data, dict):
        return {}
    out: dict[str, Any] = {}
    for k, v in odds_data.items():
        key = str(k)
        if key.startswith("_"):
            out[key] = v
            continue
        eu = coerce_float_european(v)
        if eu is not None:
            out[key] = eu
    return out


def normalize_team_name(name: str) -> str:
    text = unicodedata.normalize("NFKC", name or "").casefold()
    text = re.sub(r"[\W_]+", " ", text, flags=re.UNICODE).strip()
    # Only remove unambiguous, non-semantic suffixes; youth/women/reserve are retained.
    text = re.sub(r"\s+(?:fc|club|football club)$", "", text).strip()
    return text


def match_confidence(a: Mapping, b: Mapping, max_time_delta_seconds: int = 300) -> float:
    if a.get("sport") != b.get("sport") or a.get("period", "full_time") != b.get("period", "full_time"):
        return 0.0
    if normalize_team_name(a.get("home_team", "")) != normalize_team_name(b.get("home_team", "")):
        return 0.0
    if normalize_team_name(a.get("away_team", "")) != normalize_team_name(b.get("away_team", "")):
        return 0.0
    try:
        delta = abs((a["start_time"] - b["start_time"]).total_seconds())
    except Exception:
        delta = max_time_delta_seconds + 1
    if delta > max_time_delta_seconds:
        return 0.0
    return float(max(Decimal("0"), Decimal("0.99") - Decimal(str(delta)) / Decimal(str(max_time_delta_seconds)) * Decimal("0.09")))


def allocate_stakes(total: Decimal | int | float | str, legs: Iterable[Mapping], step: Decimal = Decimal("0.01")) -> list[dict]:
    total = Decimal(str(total))
    rows = [dict(x) for x in legs]
    if total <= 0 or not rows or step <= 0:
        raise ValueError("total and legs must be positive")
    inv = sum(Decimal(1) / Decimal(str(row["odds"])) for row in rows)
    raw = [(total * (Decimal(1) / Decimal(str(row["odds"]))) / inv) for row in rows]
    rounded = [(x / step).to_integral_value(rounding=ROUND_DOWN) * step for x in raw]
    remainder = total - sum(rounded)
    i = 0
    while remainder >= step:
        rounded[i % len(rounded)] += step
        remainder -= step
        i += 1
    for row, stake in zip(rows, rounded):
        row["stake"] = stake.quantize(step)
    return rows


def implied_probability(odds: Iterable[Decimal | int | float | str]) -> Decimal:
    return sum((Decimal(1) / Decimal(str(x)) for x in odds), Decimal(0))


def settle_profit(legs: Iterable[Mapping], outcome: str, fee_rate: Decimal = Decimal("0")) -> Decimal:
    stake = sum((Decimal(str(x["stake"])) for x in legs), Decimal(0))
    payout = sum((Decimal(str(x["stake"])) * (Decimal(str(x["odds"])) if x.get("selection") == outcome else Decimal(0)) for x in legs), Decimal(0))
    fee = payout * fee_rate
    return (payout - stake - fee).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
