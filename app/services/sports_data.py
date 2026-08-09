"""赛前上下文质量评估（维度完整度 → 置信度封顶）。

数据本身由 AI 搜索提供，本模块只负责 quality / confidence_cap。
"""
from __future__ import annotations

from typing import Optional


def compute_quality(ctx: dict) -> dict:
    """根据产品数据维度完整度生成 quality 标记。"""
    source = str(ctx.get("source") or "none")
    fields: list[str] = []
    weights = {
        "h2h": 0.30,
        "home_form": 0.20,
        "away_form": 0.20,
        "standings": 0.30,
        "analysis": 0.12,
        "live": 0.12,
        "trend": 0.08,
    }
    score = 0.0
    h2n = len((ctx.get("h2h") or {}).get("matches") or [])
    hf = len((ctx.get("home_form") or {}).get("matches") or [])
    af = len((ctx.get("away_form") or {}).get("matches") or [])
    standings = ctx.get("standings") or {}

    if h2n:
        fields.append("h2h")
        score += weights["h2h"]
    if hf:
        fields.append("home_form")
        score += weights["home_form"]
    if af:
        fields.append("away_form")
        score += weights["away_form"]
    if standings.get("home") or standings.get("away"):
        fields.append("standings")
        score += weights["standings"]
    analysis = ctx.get("analysis") if isinstance(ctx.get("analysis"), dict) else {}
    live = ctx.get("live") if isinstance(ctx.get("live"), dict) else {}
    trend = ctx.get("trend") if isinstance(ctx.get("trend"), dict) else {}

    if analysis and any(
        bool(analysis.get(k))
        for k in ("injuries", "features", "compare", "analysis_tables")
    ):
        fields.append("analysis")
        score += weights["analysis"]
    if live and any(
        bool((live.get(k) or {}).get("count")) or bool((live.get(k) or {}).get("tables"))
        for k in ("lineup", "probabilities", "half_full_stats")
    ):
        fields.append("live")
        score += weights["live"]
    if trend and (trend.get("tables") or trend.get("initial_odds")):
        fields.append("trend")
        score += weights["trend"]
    if source == "none":
        score = 0.0
    return {
        "source": source,
        "completeness": round(min(1.0, score), 3),
        "fields_present": fields,
    }


def confidence_cap_for_quality(quality: dict | None) -> Optional[float]:
    """信息不足时强制降低可用置信度上限；完整 AI 搜索数据不封顶。"""
    q = quality if isinstance(quality, dict) else {}
    source = str(q.get("source") or "none")
    try:
        comp = float(q.get("completeness") or 0)
    except (TypeError, ValueError):
        comp = 0.0
    if source == "none" or comp < 0.3:
        return 0.50
    if comp < 0.5:
        return 0.60
    return None
