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
        "h2h": 0.20,
        "home_form": 0.15,
        "away_form": 0.15,
        "injuries": 0.15,
        "player_stats": 0.12,
        "motivation": 0.10,
        "standings": 0.13,
    }
    score = 0.0
    h2n = len((ctx.get("h2h") or {}).get("matches") or [])
    hf = len((ctx.get("home_form") or {}).get("matches") or [])
    af = len((ctx.get("away_form") or {}).get("matches") or [])
    inj = len(ctx.get("news_injuries") or [])
    ps = ctx.get("player_stats") or {}
    mot = ctx.get("motivation") or {}
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
    if inj:
        fields.append("injuries")
        score += weights["injuries"]
    if (isinstance(ps, dict) and (ps.get("home") or ps.get("away"))) or len(ctx.get("player_status") or []) > 0:
        fields.append("player_stats")
        score += weights["player_stats"]
    if isinstance(mot, dict) and (mot.get("home") or mot.get("away") or mot.get("notes")):
        fields.append("motivation")
        score += weights["motivation"]
    if standings.get("home") or standings.get("away"):
        fields.append("standings")
        score += weights["standings"]
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
        return 0.55
    if comp < 0.5:
        return 0.65
    return None
