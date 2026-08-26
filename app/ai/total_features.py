"""全场大小球统一特征矩阵。

所有值均由结构化数据确定性计算，供 DeepSeek、模型后复核和日志共同使用。
模型不得自行猜测比赛时间、盘口线、赔率或 NowScore 样本结论。
"""
from __future__ import annotations

import math
from typing import Any, Optional


def _float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _int(value: Any) -> Optional[int]:
    number = _float(value)
    return int(number) if number is not None else None


def _total_market(market_odds: Any) -> dict[str, Any]:
    root = market_odds if isinstance(market_odds, dict) else {}
    markets = root.get("markets") if isinstance(root.get("markets"), dict) else {}
    if isinstance(markets.get("total"), dict):
        return markets["total"]
    if isinstance(root.get("total"), dict):
        return root["total"]
    return root


def _odds_pair(value: Any) -> dict[str, Optional[float]]:
    data = value if isinstance(value, dict) else {}
    return {"over": _float(data.get("over")), "under": _float(data.get("under"))}


def _normalized_implied(odds: dict[str, Optional[float]]) -> dict[str, Optional[float]]:
    over = odds.get("over")
    under = odds.get("under")
    if over is None or under is None or over <= 1.0 or under <= 1.0:
        return {"over": None, "under": None, "margin": None}
    raw_over = 1.0 / over
    raw_under = 1.0 / under
    total = raw_over + raw_under
    return {
        "over": round(raw_over / total, 4),
        "under": round(raw_under / total, 4),
        "margin": round(total - 1.0, 4),
    }


def _elapsed_minutes(match_info: dict) -> Optional[float]:
    try:
        from app.services.bookmakers.match_live import (
            match_elapsed_seconds,
            parse_match_clock_minutes,
        )

        seconds = match_elapsed_seconds(
            sport=str(match_info.get("sport") or ""),
            period=str(match_info.get("period") or ""),
            clock=str(match_info.get("clock") or "").strip(),
            league=str(match_info.get("league") or ""),
        )
        if seconds is not None:
            return round(max(0.0, seconds / 60.0), 3)
        minutes = parse_match_clock_minutes(
            str(match_info.get("clock") or "").strip(), allow_countdown=False,
        )
        return round(float(minutes), 3) if minutes is not None else None
    except Exception:
        return None


def _phase(sport: str, played: Optional[float], is_live: bool, full_minutes: float = 90.0) -> str:
    if not is_live:
        return "pregame"
    if played is None:
        return "unknown_live"
    if sport == "basketball":
        quarter = full_minutes / 4.0
        if played < quarter:
            return "q1"
        if played < quarter * 2:
            return "q2"
        if played < quarter * 3:
            return "q3"
        if played < full_minutes * 0.9167:
            return "q4"
        return "q4_final"
    if played < 20:
        return "early"
    if played < 45:
        return "first_half"
    if played < 60:
        return "second_half_early"
    if played < 75:
        return "late"
    if played < 88:
        return "closing"
    return "stoppage"


def _dimension_matrix(ctx: dict) -> dict[str, Any]:
    quality = ctx.get("quality") if isinstance(ctx.get("quality"), dict) else {}
    analysis = ctx.get("analysis") if isinstance(ctx.get("analysis"), dict) else {}
    trend = ctx.get("trend") if isinstance(ctx.get("trend"), dict) else {}
    performance = trend.get("performance") if isinstance(trend.get("performance"), dict) else {}
    market = trend.get("market_odds") if isinstance(trend.get("market_odds"), dict) else {}
    standings = ctx.get("standings") if isinstance(ctx.get("standings"), dict) else {}
    return {
        "h2h_samples": len((ctx.get("h2h") or {}).get("matches") or []),
        "home_form_samples": len((ctx.get("home_form") or {}).get("matches") or []),
        "away_form_samples": len((ctx.get("away_form") or {}).get("matches") or []),
        "standings": bool(standings.get("home") or standings.get("away")),
        "injuries": bool(analysis.get("injuries")),
        "team_features": bool(analysis.get("features") or analysis.get("compare")),
        "performance_trend": bool(performance.get("available") or trend.get("tables")),
        "nowscore_company_odds": bool(market.get("available")),
        "identity_valid": quality.get("identity_valid", (ctx.get("identity") or {}).get("validated") is True),
        "fresh": quality.get("fresh"),
        "completeness": _float(quality.get("completeness")) or 0.0,
    }


def build_total_feature_matrix(
    match_info: Optional[dict],
    market_odds: Optional[dict],
    historical_data: Optional[dict],
    *,
    line: Any,
    line_source: str,
) -> dict[str, Any]:
    """构建统一的基本面、亚洲盘口、比赛时间与节奏特征。"""
    info = match_info if isinstance(match_info, dict) else {}
    ctx = historical_data if isinstance(historical_data, dict) else {}
    sport = "basketball" if "basket" in str(info.get("sport") or "").lower() else "football"
    if sport == "basketball":
        from app.ai.league_focus import basketball_regulation_minutes

        full_minutes = basketball_regulation_minutes(str(info.get("league") or ""))
    else:
        full_minutes = 90.0
    line_value = _float(line)

    market = _total_market(market_odds)
    market_root = market_odds if isinstance(market_odds, dict) else {}
    line_movements = market_root.get("line_movements") if isinstance(market_root.get("line_movements"), dict) else {}
    movement = line_movements.get("total") if isinstance(line_movements.get("total"), dict) else {}
    current_line = _float(market.get("line"))
    if current_line is None:
        current_line = line_value
    current_odds = _odds_pair(market.get("odds"))
    if current_odds["over"] is None and current_odds["under"] is None:
        current_odds = _odds_pair(info.get("odds"))
    opening = movement.get("opening") if isinstance(movement.get("opening"), dict) else {}
    if not opening:
        opening = market.get("opening") if isinstance(market.get("opening"), dict) else {}
    movement_current = movement.get("current") if isinstance(movement.get("current"), dict) else {}
    opening_line = _float(opening.get("line"))
    opening_odds = _odds_pair(opening.get("odds"))
    movement_current_odds = _odds_pair(movement_current.get("odds"))
    implied = _normalized_implied(current_odds)
    line_delta = round(current_line - opening_line, 3) if current_line is not None and opening_line is not None else None
    odds_delta = {
        side: round(float(current_odds[side]) - float(opening_odds[side]), 4)
        if current_odds[side] is not None and opening_odds[side] is not None else None
        for side in ("over", "under")
    }
    movement_odds_delta = {
        side: round(float(movement_current_odds[side]) - float(opening_odds[side]), 4)
        if movement_current_odds[side] is not None and opening_odds[side] is not None else None
        for side in ("over", "under")
    }

    home_score = _int(info.get("home_score"))
    away_score = _int(info.get("away_score"))
    status = str(info.get("status") or info.get("match_status") or "").strip().lower()
    explicit_upcoming = status in ("upcoming", "prematch", "pre_match", "not_started", "未开始")
    explicit_live = bool(info.get("is_live")) or status in (
        "live", "inplay", "in_play", "running", "started", "进行中", "滚球",
    )
    has_live_clock = bool(str(info.get("clock") or "").strip() or str(info.get("period") or "").strip())
    is_live = False if explicit_upcoming else bool(
        explicit_live or has_live_clock or home_score is not None or away_score is not None
    )
    current_total = (home_score or 0) + (away_score or 0) if is_live else 0
    played = _elapsed_minutes(info) if is_live else None
    remain = max(0.0, full_minutes - played) if played is not None else None

    line_move_type = "unavailable"
    line_direction = "neutral"
    if line_delta is not None:
        if line_delta >= 0.25:
            line_move_type = "score_adjustment" if is_live and opening_line is not None and current_total >= opening_line else "market_up"
            line_direction = "neutral" if line_move_type == "score_adjustment" else "over"
        elif line_delta <= -0.25:
            line_move_type = "market_down"
            line_direction = "under"
        else:
            line_move_type = "stable"

    movement_price_direction = "neutral"
    if (
        movement_odds_delta["over"] is not None
        and movement_odds_delta["under"] is not None
    ):
        if movement_odds_delta["over"] <= -0.02 and movement_odds_delta["under"] >= 0.02:
            movement_price_direction = "over"
        elif movement_odds_delta["under"] <= -0.02 and movement_odds_delta["over"] >= 0.02:
            movement_price_direction = "under"

    price_direction = movement_price_direction
    if implied["over"] is not None and implied["under"] is not None:
        probability_gap = float(implied["over"]) - float(implied["under"])
        if price_direction == "neutral" and probability_gap >= 0.03:
            price_direction = "over"
        elif price_direction == "neutral" and probability_gap <= -0.03:
            price_direction = "under"

    market_direction = line_direction if line_direction != "neutral" else price_direction
    market_conflict = bool(
        line_direction in ("over", "under")
        and price_direction in ("over", "under")
        and line_direction != price_direction
    )
    if market_conflict:
        market_direction = "conflict"

    pace: dict[str, Any] = {
        "available": played is not None and line_value is not None,
        "played_minutes": played,
        "remaining_minutes": round(remain, 3) if remain is not None else None,
        "full_minutes": full_minutes,
        "current_total": current_total,
        "line": line_value,
        "remaining_to_line": round(line_value - current_total, 3) if line_value is not None else None,
        "direction": "neutral",
        "reliability": "none",
    }
    if played is not None and played > 0 and line_value is not None:
        actual_rate = current_total / played
        line_rate = line_value / full_minutes
        linear_projection = actual_rate * full_minutes
        if current_total == 0:
            baseline = 150.0 if sport == "basketball" else 2.5
            adjusted_projection = baseline * max(0.0, full_minutes - played) / full_minutes
            pace["zero_score_baseline"] = baseline
        else:
            factor = 1.0
            if sport == "football":
                if played >= 75:
                    factor = 0.55
                elif played >= 60:
                    factor = 0.75
                elif played >= 50:
                    factor = 0.85
            elif played >= full_minutes * 0.9167:
                factor = 1.05
            adjusted_projection = current_total + actual_rate * max(0.0, full_minutes - played) * factor
            pace["remaining_rate_factor"] = factor
        threshold = max(5.0, line_value * 0.035) if sport == "basketball" else 0.45
        projection_delta = adjusted_projection - line_value
        min_reliable = 5.0 if sport == "basketball" else 10.0
        reliability = "low" if played < min_reliable or current_total == 0 else "medium"
        if played >= full_minutes * 0.5 and current_total > 0:
            reliability = "high"
        direction = "neutral"
        if reliability != "low":
            if projection_delta >= threshold:
                direction = "over"
            elif projection_delta <= -threshold:
                direction = "under"
        pace.update({
            "actual_rate": round(actual_rate, 4),
            "line_rate": round(line_rate, 4),
            "rate_deviation_pct": round((actual_rate - line_rate) / line_rate * 100.0, 2) if line_rate > 0 else None,
            "linear_projection": round(linear_projection, 3),
            "adjusted_projection": round(adjusted_projection, 3),
            "projection_delta": round(projection_delta, 3),
            "direction": direction,
            "reliability": reliability,
            "needed_over_rate": round(max(0.0, line_value - current_total) / remain, 4) if remain and remain > 0 else None,
        })

    evidence = ctx.get("total_market_evidence") if isinstance(ctx.get("total_market_evidence"), dict) else {}
    evidence_consensus = evidence.get("consensus") if isinstance(evidence.get("consensus"), dict) else {}
    directions = {
        "asian_market": market_direction,
        "live_pace": str(pace.get("direction") or "neutral"),
        "nowscore_fundamentals": str(evidence_consensus.get("direction") or "neutral"),
    }
    over_support = [key for key, value in directions.items() if value == "over"]
    under_support = [key for key, value in directions.items() if value == "under"]
    conflicts: list[str] = []
    if market_conflict:
        conflicts.append("asian_line_vs_price")
    if over_support and under_support:
        conflicts.append("cross_dimension_direction_conflict")
    if evidence_consensus.get("home_away_conflict") is True:
        conflicts.append("nowscore_home_away_form_conflict")
    consensus_direction = "neutral"
    if len(over_support) >= 2 and not under_support:
        consensus_direction = "over"
    elif len(under_support) >= 2 and not over_support:
        consensus_direction = "under"

    hard_failures: list[str] = []
    if line_value is None:
        hard_failures.append("missing_total_line")
    if current_odds["over"] is None or current_odds["under"] is None:
        hard_failures.append("missing_two_sided_total_odds")
    if evidence.get("usable") is not True:
        hard_failures.append("nowscore_evidence_unusable")
    if is_live and played is None:
        hard_failures.append("missing_live_match_clock")
    # OB API 快照带原生赛事/盘口质量标记。缺少快照时兼容历史缓存，
    # 但一旦明确标记为不完整，禁止模型用不完整的方向数据下注。
    provider_code = str(info.get("provider_code") or "").strip().lower()
    snapshots = info.get("provider_snapshots") if isinstance(info.get("provider_snapshots"), dict) else {}
    ob_snapshot = snapshots.get("ob") if isinstance(snapshots.get("ob"), dict) else None
    if provider_code == "ob" and ob_snapshot:
        if ob_snapshot.get("total_two_sided") is not True:
            hard_failures.append("ob_total_snapshot_incomplete")
        if ob_snapshot.get("sport_valid") is False:
            hard_failures.append("ob_sport_snapshot_invalid")

    return {
        "version": 1,
        "sport": sport,
        "dimensions": _dimension_matrix(ctx),
        "match_state": {
            "is_live": is_live,
            "phase": _phase(sport, played, is_live, full_minutes),
            "period": str(info.get("period") or ""),
            "clock": str(info.get("clock") or ""),
            "played_minutes": played,
            "home_score": home_score,
            "away_score": away_score,
            "current_total": current_total,
        },
        "asian_total_market": {
            "line": line_value,
            "line_source": line_source,
            "opening_line": opening_line,
            "line_delta": line_delta,
            "line_move_type": line_move_type,
            "line_direction": line_direction,
            "current_odds": current_odds,
            "opening_odds": opening_odds,
            "movement_provider": movement.get("provider"),
            "movement_current_line": _float(movement_current.get("line")),
            "movement_current_odds": movement_current_odds,
            "movement_odds_delta": movement_odds_delta,
            "odds_delta": odds_delta,
            "normalized_implied_probability": implied,
            "price_direction": price_direction,
            "movement_price_direction": movement_price_direction,
            "direction": market_direction,
            "conflict": market_conflict,
        },
        "pace": pace,
        "nowscore": {
            "usable": evidence.get("usable") is True,
            "line": evidence.get("line"),
            "buckets": evidence.get("buckets") or {},
            "consensus": evidence_consensus,
            "warnings": evidence.get("warnings") or [],
        },
        "provider_snapshot": {
            "provider": provider_code,
            "ob": ob_snapshot,
        },
        "directional_summary": {
            "by_dimension": directions,
            "over_support": over_support,
            "under_support": under_support,
            "consensus_direction": consensus_direction,
            "conflicts": conflicts,
        },
        "gates": {
            "analysis_ready": not hard_failures,
            "hard_failures": hard_failures,
        },
    }
