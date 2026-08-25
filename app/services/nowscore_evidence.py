"""NowScore 基本面数据的校验与精确大小球盘口对照。

NowScore 负责历史赛果、近期状态等基本面；当前大小球盘口始终来自投注平台。
本模块不猜测盘口，也不调用模型，只把历史总分按当前精确盘口重新统计。
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
import statistics
from typing import Any, Optional


def _as_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _parse_time(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _row_total(row: Any) -> Optional[float]:
    if not isinstance(row, dict):
        return None
    home = _as_float(row.get("home_goals"))
    away = _as_float(row.get("away_goals"))
    if home is None or away is None or home < 0 or away < 0:
        return None
    return home + away


def _totals(bucket: Any, limit: int = 10) -> list[float]:
    if not isinstance(bucket, dict) or not isinstance(bucket.get("matches"), list):
        return []
    result: list[float] = []
    for row in bucket["matches"][: max(0, limit)]:
        total = _row_total(row)
        if total is not None:
            result.append(total)
    return result


def _bucket_stats(values: list[float], line: float) -> dict[str, Any]:
    over = sum(1 for value in values if value > line)
    under = sum(1 for value in values if value < line)
    push = len(values) - over - under
    decided = over + under
    return {
        "sample_size": len(values),
        "average_total": round(statistics.fmean(values), 3) if values else None,
        "median_total": round(statistics.median(values), 3) if values else None,
        "stddev_total": round(statistics.pstdev(values), 3) if len(values) > 1 else 0.0 if values else None,
        "minimum_total": min(values) if values else None,
        "maximum_total": max(values) if values else None,
        "over": over,
        "under": under,
        "push": push,
        "over_rate": round(over / decided, 3) if decided else None,
        "under_rate": round(under / decided, 3) if decided else None,
        "line": line,
    }


def _line_is_plausible(line: float, sport: str) -> bool:
    if sport == "basketball":
        return 40.0 <= line <= 400.0
    return 0.25 <= line <= 15.0


def build_total_market_evidence(
    ctx: Optional[dict],
    *,
    line: Any,
    sport: str,
    line_source: str = "bookmaker_live_total",
    max_age_sec: int = 21600,
    min_form_samples: int = 3,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """校验 NowScore 上下文，并按投注平台当前盘口统计 over/under/push。

    ``usable`` 是生产闸门字段：球队映射、球种、时效、盘口与双方近期样本均合格才为真。
    H2H 是补充证据，不作为可用性的必要条件。
    """
    context = ctx if isinstance(ctx, dict) else {}
    sport_norm = "basketball" if "basket" in str(sport or "").lower() else "football"
    line_value = _as_float(line)
    warnings: list[str] = []

    source_ok = str(context.get("source") or "none").strip().lower() == "nowscore"
    if not source_ok:
        warnings.append("source_not_nowscore")

    context_sport = "basketball" if "basket" in str(context.get("sport") or "").lower() else "football"
    sport_valid = bool(context.get("sport")) and context_sport == sport_norm
    if not sport_valid:
        warnings.append("sport_mismatch")

    identity = context.get("identity") if isinstance(context.get("identity"), dict) else {}
    identity_valid = identity.get("validated") is True
    if not identity_valid:
        warnings.append("team_identity_unverified")

    fetched_at = _parse_time(context.get("fetched_at"))
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    age_sec = max(0.0, (current.astimezone(timezone.utc) - fetched_at).total_seconds()) if fetched_at else None
    fresh = age_sec is not None and age_sec <= max(1, int(max_age_sec))
    if not fresh:
        warnings.append("context_stale_or_missing_timestamp")

    line_valid = line_value is not None and _line_is_plausible(line_value, sport_norm)
    if not line_valid:
        warnings.append("invalid_bookmaker_total_line")

    buckets: dict[str, dict[str, Any]] = {}
    for key in ("h2h", "home_form", "away_form"):
        values = _totals(context.get(key))
        buckets[key] = _bucket_stats(values, line_value) if line_valid else {
            "sample_size": len(values),
            "line": line_value,
        }

    home_n = int(buckets["home_form"].get("sample_size") or 0)
    away_n = int(buckets["away_form"].get("sample_size") or 0)
    samples_valid = home_n >= min_form_samples and away_n >= min_form_samples
    if home_n < min_form_samples:
        warnings.append("insufficient_home_form_samples")
    if away_n < min_form_samples:
        warnings.append("insufficient_away_form_samples")

    direction = "neutral"
    over_score = None
    under_score = None
    conflict = False
    if line_valid:
        weighted: list[tuple[float, dict[str, Any]]] = [
            (0.4, buckets["home_form"]),
            (0.4, buckets["away_form"]),
        ]
        if int(buckets["h2h"].get("sample_size") or 0) >= min_form_samples:
            weighted.append((0.2, buckets["h2h"]))
        available = [(weight, item) for weight, item in weighted if item.get("over_rate") is not None]
        weight_sum = sum(weight for weight, _ in available)
        if weight_sum:
            over_score = round(sum(weight * float(item["over_rate"]) for weight, item in available) / weight_sum, 3)
            under_score = round(1.0 - over_score, 3)
            delta = over_score - under_score
            if over_score >= 0.60 and delta >= 0.12:
                direction = "over"
            elif under_score >= 0.60 and -delta >= 0.12:
                direction = "under"

        home_over = buckets["home_form"].get("over_rate")
        away_over = buckets["away_form"].get("over_rate")
        if home_over is not None and away_over is not None:
            conflict = (home_over >= 0.67 and away_over <= 0.33) or (away_over >= 0.67 and home_over <= 0.33)
            if conflict:
                warnings.append("home_away_form_conflict")

    usable = bool(source_ok and sport_valid and identity_valid and fresh and line_valid and samples_valid)
    return {
        "source": "nowscore",
        "market_line_source": str(line_source or "bookmaker_total_snapshot"),
        "line": line_value,
        "sport": sport_norm,
        "usable": usable,
        "identity_valid": identity_valid,
        "sport_valid": sport_valid,
        "fresh": fresh,
        "age_sec": round(age_sec, 1) if age_sec is not None else None,
        "samples_valid": samples_valid,
        "minimum_form_samples": min_form_samples,
        "buckets": buckets,
        "consensus": {
            "direction": direction,
            "over_score": over_score,
            "under_score": under_score,
            "home_away_conflict": conflict,
        },
        "warnings": warnings,
    }


def evidence_gate_reason(evidence: Any) -> str:
    if not isinstance(evidence, dict) or evidence.get("usable") is not True:
        warnings = evidence.get("warnings") if isinstance(evidence, dict) else []
        suffix = ",".join(str(item) for item in (warnings or [])[:4])
        return f"nowscore_evidence_unusable:{suffix}" if suffix else "nowscore_evidence_unusable"
    return ""
