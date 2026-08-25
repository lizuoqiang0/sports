"""赛前上下文质量评估（样本质量与维度完整度 → 置信度封顶）。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


def compute_quality(ctx: dict) -> dict:
    """按真实样本量、身份校验、时效与有效维度生成质量标记。"""
    source = str(ctx.get("source") or "none")
    fields: list[str] = []
    score = 0.0
    h2n = len((ctx.get("h2h") or {}).get("matches") or [])
    hf = len((ctx.get("home_form") or {}).get("matches") or [])
    af = len((ctx.get("away_form") or {}).get("matches") or [])
    standings = ctx.get("standings") or {}

    if h2n:
        fields.append("h2h")
        score += 0.15 * min(1.0, h2n / 5.0)
    if hf:
        fields.append("home_form")
        score += 0.20 * min(1.0, hf / 5.0)
    if af:
        fields.append("away_form")
        score += 0.20 * min(1.0, af / 5.0)
    if standings.get("home") or standings.get("away"):
        fields.append("standings")
        both_sides = bool(standings.get("home") and standings.get("away"))
        score += 0.15 if both_sides else 0.075
    analysis = ctx.get("analysis") if isinstance(ctx.get("analysis"), dict) else {}
    trend = ctx.get("trend") if isinstance(ctx.get("trend"), dict) else {}

    if analysis and any(
        bool(analysis.get(k))
        for k in ("injuries", "features", "compare", "analysis_tables")
    ):
        fields.append("analysis")
        analysis_sections = sum(
            1 for key in ("injuries", "features", "compare", "analysis_tables")
            if analysis.get(key)
        )
        score += 0.15 * min(1.0, analysis_sections / 3.0)
    performance_available = bool(
        (trend.get("performance") or {}).get("available")
        if isinstance(trend.get("performance"), dict)
        else trend.get("tables")
    )
    market_available = bool(
        (trend.get("market_odds") or {}).get("available")
        if isinstance(trend.get("market_odds"), dict)
        else trend.get("initial_odds")
    )
    if performance_available or market_available:
        fields.append("trend")
        score += 0.10 if performance_available else 0.0
        score += 0.05 if market_available else 0.0

    identity = ctx.get("identity") if isinstance(ctx.get("identity"), dict) else {}
    identity_valid = identity.get("validated") is True
    fetched_at = None
    try:
        fetched_at = datetime.fromisoformat(str(ctx.get("fetched_at") or "").replace("Z", "+00:00"))
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        fetched_at = None
    age_sec = max(0.0, (datetime.now(timezone.utc) - fetched_at).total_seconds()) if fetched_at else None
    try:
        from app.config import settings

        max_age_sec = int(getattr(settings, "NOWSCORE_MAX_CONTEXT_AGE_SEC", 21600) or 21600)
    except Exception:
        max_age_sec = 21600
    fresh = age_sec is not None and age_sec <= max_age_sec

    if source == "none":
        score = 0.0
    return {
        "source": source,
        "completeness": round(min(1.0, score), 3),
        "fields_present": fields,
        "sample_counts": {"h2h": h2n, "home_form": hf, "away_form": af},
        "identity_valid": identity_valid,
        "fresh": fresh,
        "age_sec": round(age_sec, 1) if age_sec is not None else None,
        "market_odds_available": market_available,
        "performance_trend_available": performance_available,
    }


def confidence_cap_for_quality(quality: dict | None) -> Optional[float]:
    """信息不足时强制降低可用置信度上限；完整 AI 搜索数据不封顶。"""
    q = quality if isinstance(quality, dict) else {}
    source = str(q.get("source") or "none")
    try:
        comp = float(q.get("completeness") or 0)
    except (TypeError, ValueError):
        comp = 0.0
    if source == "none" or comp < 0.30:
        return 0.50
    if comp < 0.50:
        return 0.60
    if not q.get("identity_valid", False) or not q.get("fresh", False):
        return 0.55
    return None
