"""盘口推荐：OB/平博单边，仅生成全场大小球推荐。"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import BetType, Match, Odds
from app.services.bookmakers.catalog import BOOKMAKER_CATALOG, provider_name
from app.services.provider_utils import best_by_selection, code_by_provider

logger = logging.getLogger(__name__)

ALIAS = {
    "OB Sports": "OB体育",
    "OB": "OB体育",
    "Pinnacle": "平博",
}

SEL_LABELS = {
    "under": "小球",
    "over": "大球",
    "home": "主",
    "away": "客",
    "draw": "平",
}

BET_TYPE_ENUM = {
    "total": BetType.TOTAL,
    "moneyline": BetType.MONEYLINE,
    "spread": BetType.SPREAD,
}

SPORT_MARKETS: dict[str, list[dict[str, Any]]] = {
    # 全场大小球（total under/over 双向）。
    "football": [
        {
            "key": "ft_ou",
            "bet_type": "total",
            "label": "全场大小球",
            "selections": ("under", "over"),
        },
    ],
    "basketball": [
        {
            "key": "ft_ou",
            "bet_type": "total",
            "label": "全场大小球",
            "selections": ("under", "over"),
        },
    ],
}

SINGLE_SIDE_NAMES = {"OB体育", "平博"}


def _normalize_sport(sport: str) -> str:
    s = (sport or "").lower()
    if s in ("football", "soccer"):
        return "football"
    if s in ("basketball",):
        return "basketball"
    return s


def _normalize_bet_type(bet_type: str) -> str:
    bt = str(bet_type or "").strip().lower()
    if bt in ("1x2", "ml", "match_odds", "win"):
        return "moneyline"
    if bt in ("ah", "handicap", "asian_handicap", "spread"):
        return "spread"
    if bt in ("ou", "totals", "total"):
        return "total"
    return bt


def normalize_prediction(raw: Any, *, bet_type: str = "") -> str:
    """规范预测方向；无法识别返回空串。"""
    s = str(raw or "").strip().lower()
    bt = _normalize_bet_type(bet_type)
    aliases = {
        "under": "under",
        "u": "under",
        "小": "under",
        "小球": "under",
        "over": "over",
        "o": "over",
        "大": "over",
        "大球": "over",
        "home": "home",
        "away": "away",
        "draw": "draw",
        "主": "home",
        "客": "away",
        "平": "draw",
        "主胜": "home",
        "客胜": "away",
        "平局": "draw",
        "h": "home",
        "a": "away",
        "d": "draw",
        "x": "draw",
        "1": "home",
        "2": "away",
        "让主": "home",
        "让客": "away",
    }
    if s in aliases:
        pred = aliases[s]
    elif "小球" in s or s == "小" or "under" in s:
        pred = "under"
    elif "大球" in s or s == "大" or "over" in s:
        pred = "over"
    elif "主胜" in s or s in ("主", "home"):
        pred = "home"
    elif "客胜" in s or s in ("客", "away"):
        pred = "away"
    elif "平" in s or "draw" in s:
        pred = "draw"
    else:
        pred = ""

    if not pred:
        return ""
    if bt == "total" and pred not in ("under", "over"):
        return ""
    if bt == "moneyline" and pred not in ("home", "away", "draw"):
        return ""
    if bt == "spread" and pred not in ("home", "away"):
        return ""
    if not bt and pred not in ("under", "home", "away", "draw"):
        return ""
    return pred


def _side_probs(
    prediction: str,
    confidence: float,
    selections: tuple[str, ...] | list[str],
    *,
    bet_type: str = "",
) -> dict[str, float]:
    """由模型方向+置信度展开各侧胜率。"""
    try:
        conf = float(confidence)
    except (TypeError, ValueError):
        conf = 0.5
    conf = max(0.01, min(0.99, conf))
    sels = [str(s) for s in selections]
    pred = normalize_prediction(prediction, bet_type=bet_type) or str(prediction or "").lower()
    if not sels:
        return {}
    if pred not in sels:
        # 均分
        share = round(1.0 / len(sels), 6)
        return {s: share for s in sels}
    others = [s for s in sels if s != pred]
    if not others:
        return {pred: conf}
    rest = round((1.0 - conf) / len(others), 6)
    out = {pred: conf}
    for s in others:
        out[s] = rest
    return out


async def load_market_matrix(
    db: AsyncSession,
    match_id: int,
    bet_type: str,
    *,
    providers_filter: set[str] | None = None,
) -> tuple[dict[str, dict[str, float]], float, float]:
    """返回 (matrix[selection][provider]=odds, spread_line, total_line)。"""
    bt = _normalize_bet_type(bet_type)
    enum = BET_TYPE_ENUM.get(bt)
    if enum is None:
        return {}, 0.0, 0.0

    allowed = set(SINGLE_SIDE_NAMES)
    if providers_filter:
        allowed = {p for p in allowed if p in providers_filter}

    # 跨站比价：平博赔率存在同场兄弟 match_id 名下（ob/pinnacle 各一条 Match 记录），
    # 只查 canonical 自己的行会导致矩阵里永远只有 canonical 站 → 比价失效
    match_ids = [int(match_id)]
    try:
        from app.models.user import Match
        from app.services.fixture_key import sibling_match_ids

        m = await db.get(Match, int(match_id))
        if m is not None:
            sib = await sibling_match_ids(db, m)
            for sid in sib:
                if sid and int(sid) not in match_ids:
                    match_ids.append(int(sid))
    except Exception:
        match_ids = [int(match_id)]

    result = await db.execute(
        select(Odds).where(
            and_(
                Odds.match_id.in_(match_ids),
                Odds.bet_type == enum,
                Odds.valid_to.is_(None),
                # AI 与自动下单必须消费同一轮实时快照；长期悬挂的 valid_to=NULL
                # 旧行不能继续参与方向判断或跨站比价。
                Odds.is_live.is_(True),
                Odds.valid_from >= (
                    datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5)
                ),
            )
        ).order_by(
            # canonical 站的行优先（盘口线以其为准），同站内最新行优先
            (Odds.match_id != int(match_id)),
            Odds.id.desc(),
        )
    )
    matrix: dict[str, dict[str, float]] = {}
    rows = list(result.scalars().all())
    # OB 的采集快照是盘口与赛事身份的最终质量门。历史版本可能留下
    # valid_to=NULL 的半场/锁盘 TOTAL；只要快照明确不满足“全场 + 双向”，
    # 即使数据库行仍存在，也不能进入 AI 矩阵或下单候选。
    quality_by_match: dict[int, dict[str, Any]] = {}
    try:
        qres = await db.execute(
            select(Match.id, Match.external_id, Match.extra_data).where(
                Match.id.in_(match_ids)
            )
        )
        for mid, external_id, extra_data in qres.all():
            ext = str(external_id or "").lower()
            if not ext.startswith("ob:"):
                continue
            snapshots = extra_data.get("provider_snapshots") if isinstance(extra_data, dict) else None
            snapshot = snapshots.get("ob") if isinstance(snapshots, dict) else None
            if isinstance(snapshot, dict):
                quality_by_match[int(mid)] = snapshot
    except Exception:
        # 兼容旧测试/旧数据库结构；没有快照时仍由其它实时闸门校验。
        quality_by_match = {}
    # 盘口线是分析的锚点：优先使用当前 match（通常是用户选中的站点）的线，
    # 只有同一条线才允许跨站比价，防止 OB 2.5 的赔率搭配到平博 2.75 的线。
    anchor_line: float | None = None
    for row in rows:
        if int(getattr(row, "match_id", 0) or 0) != int(match_id):
            continue
        try:
            value = float(row.total if bt == "total" else row.spread)
        except (TypeError, ValueError):
            continue
        if value > 0:
            anchor_line = value
            break
    if anchor_line is None:
        for row in rows:
            try:
                value = float(row.total if bt == "total" else row.spread)
            except (TypeError, ValueError):
                continue
            if value > 0:
                anchor_line = value
                break
    spread_line = 0.0
    total_line = 0.0
    sel_allow = {
        # 大小球双向：over 水位与 under 同源同存（Odds.odds_data 双向齐备），
        # AI 分析需要 over 即时水位计算初→即时差（此前被过滤导致大球分析缺水）
        "total": ("under", "over"),
        "moneyline": ("home", "away", "draw"),
        "spread": ("home", "away"),
    }.get(bt, ())

    for row in rows:
        pname = ALIAS.get(str(row.provider or ""), str(row.provider or ""))
        if pname not in allowed:
            code = code_by_provider(pname) or ""
            pname = provider_name(code) if code in BOOKMAKER_CATALOG else pname
        if pname not in allowed:
            continue
        if pname == "OB体育":
            snapshot = quality_by_match.get(int(getattr(row, "match_id", 0) or 0))
            if snapshot and (
                snapshot.get("full_time_total") is not True
                or snapshot.get("total_two_sided") is not True
            ):
                logger.info(
                    "skip incomplete OB total snapshot match=%s full_time=%s two_sided=%s",
                    getattr(row, "match_id", None),
                    snapshot.get("full_time_total"),
                    snapshot.get("total_two_sided"),
                )
                continue
        if anchor_line is not None:
            try:
                row_line = float(row.total if bt == "total" else row.spread)
            except (TypeError, ValueError):
                row_line = None
            if row_line is None or abs(row_line - anchor_line) > 0.01:
                logger.info(
                    "skip mismatched %s line provider=%s match=%s row_line=%s anchor=%s",
                    bt, pname, getattr(row, "match_id", None), row_line, anchor_line,
                )
                continue
        # 线取「首个非空行」并锁定（查询已按 canonical 站优先 + 最新行排序）：
        # 原先每行无条件覆盖（最后一行=兄弟站最旧行胜出），会使 AI 分析用的线
        # 与最终下单行的线错配，半分之差即可翻转 won/push
        if row.total is not None and total_line == 0.0:
            try:
                total_line = float(row.total)
            except (TypeError, ValueError):
                pass
        if row.spread is not None and spread_line == 0.0:
            try:
                spread_line = float(row.spread)
            except (TypeError, ValueError):
                pass
        for sel, val in (row.odds_data or {}).items():
            if str(sel).startswith("_") or str(sel) not in sel_allow:
                continue
            try:
                f = float(val)
            except (TypeError, ValueError):
                continue
            if f > 1.0:
                matrix.setdefault(str(sel), {})[pname] = f
    return matrix, spread_line, total_line


def _row_line(row: Odds, bet_type: str) -> float | None:
    bt = _normalize_bet_type(bet_type)
    try:
        if bt == "total" and row.total is not None:
            return float(row.total)
        if bt == "spread" and row.spread is not None:
            return float(row.spread)
    except (TypeError, ValueError):
        return None
    return None


def _public_odds(row: Odds) -> dict[str, float]:
    out: dict[str, float] = {}
    for k, v in (row.odds_data or {}).items():
        if str(k).startswith("_"):
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f > 1.0:
            out[str(k)] = f
    return out


def _line_move_direction(opening_line: float | None, current_line: float | None, bet_type: str) -> str:
    if opening_line is None or current_line is None:
        return "unknown"
    delta = float(current_line) - float(opening_line)
    if abs(delta) < 0.01:
        return "stable"
    bt = _normalize_bet_type(bet_type)
    if bt == "total":
        return "line_up" if delta > 0 else "line_down"
    if bt == "spread":
        # 主队让球线变大（更负或更正）用数值差描述；正差=主队让球减弱/受让增加
        return "home_line_up" if delta > 0 else "home_line_down"
    return "moved"


def summarize_line_movement(versions: list[Odds], *, bet_type: str) -> dict[str, Any] | None:
    """由版本序列生成初盘/即时盘/变化摘要。"""
    if not versions:
        return None
    ordered = sorted(versions, key=lambda r: r.valid_from or datetime.min)
    opening = ordered[0]
    current = next((r for r in reversed(ordered) if r.valid_to is None), ordered[-1])
    open_odds = _public_odds(opening)
    cur_odds = _public_odds(current)
    open_line = _row_line(opening, bet_type)
    cur_line = _row_line(current, bet_type)
    odds_delta: dict[str, float] = {}
    for k in set(open_odds) | set(cur_odds):
        if k in open_odds and k in cur_odds:
            odds_delta[k] = round(cur_odds[k] - open_odds[k], 3)
    line_delta = None
    if open_line is not None and cur_line is not None:
        line_delta = round(float(cur_line) - float(open_line), 3)
    moves: list[dict[str, Any]] = []
    prev = ordered[0]
    for row in ordered[1:]:
        prev_line = _row_line(prev, bet_type)
        cur_l = _row_line(row, bet_type)
        entry: dict[str, Any] = {
            "at": row.valid_from.isoformat() if row.valid_from else None,
            "odds": _public_odds(row),
        }
        if cur_l is not None:
            entry["line"] = cur_l
        if prev_line is not None and cur_l is not None and abs(cur_l - prev_line) >= 0.01:
            entry["line_delta"] = round(cur_l - prev_line, 3)
        moves.append(entry)
        prev = row
    return {
        "opening": {
            "odds": open_odds,
            "line": open_line,
            "at": opening.valid_from.isoformat() if opening.valid_from else None,
            "is_live": bool(opening.is_live),
        },
        "current": {
            "odds": cur_odds,
            "line": cur_line,
            "at": current.valid_from.isoformat() if current.valid_from else None,
            "is_live": bool(current.is_live),
        },
        "line_delta": line_delta,
        "odds_delta": odds_delta,
        "direction": _line_move_direction(open_line, cur_line, bet_type),
        "change_count": max(0, len(ordered) - 1),
        "recent_moves": moves[-5:],
    }


async def load_market_line_movement(
    db: AsyncSession,
    match_id: int,
    bet_type: str,
    *,
    providers_filter: set[str] | None = None,
) -> dict[str, Any] | None:
    """取该盘口各站点合并后的初盘→即时盘变化（优先 OB/平博）。"""
    bt = _normalize_bet_type(bet_type)
    enum = BET_TYPE_ENUM.get(bt)
    if enum is None:
        return None
    allowed = set(SINGLE_SIDE_NAMES)
    if providers_filter:
        allowed = {p for p in allowed if p in providers_filter}

    result = await db.execute(
        select(Odds)
        .where(and_(Odds.match_id == match_id, Odds.bet_type == enum))
        .order_by(Odds.valid_from.asc())
    )
    by_provider: dict[str, list[Odds]] = {}
    for row in result.scalars().all():
        pname = ALIAS.get(str(row.provider or ""), str(row.provider or ""))
        if pname not in allowed:
            code = code_by_provider(pname) or ""
            pname = provider_name(code) if code in BOOKMAKER_CATALOG else pname
        if pname not in allowed:
            continue
        by_provider.setdefault(pname, []).append(row)

    if not by_provider:
        return None

    # 优先选变化更丰富的站点；同级优先 OB
    preferred = ("OB体育", "平博")
    best_name = None
    best_rows: list[Odds] = []
    for name in preferred:
        if name in by_provider:
            best_name = name
            best_rows = by_provider[name]
            break
    if not best_rows:
        best_name, best_rows = max(by_provider.items(), key=lambda kv: len(kv[1]))

    summary = summarize_line_movement(best_rows, bet_type=bt)
    if not summary:
        return None
    summary["provider"] = best_name
    return summary


async def load_odds_history_rows(
    db: AsyncSession,
    match_id: int,
    *,
    bet_type: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """REST 历史：按 valid_from 倒序返回版本行。"""
    cond = [Odds.match_id == match_id]
    if bet_type:
        bt = _normalize_bet_type(bet_type)
        enum = BET_TYPE_ENUM.get(bt)
        if enum is not None:
            cond.append(Odds.bet_type == enum)
    result = await db.execute(
        select(Odds)
        .where(and_(*cond))
        .order_by(Odds.valid_from.desc())
        .limit(limit)
    )
    rows = []
    for o in result.scalars().all():
        rows.append({
            "id": o.id,
            "bet_type": o.bet_type.value if hasattr(o.bet_type, "value") else str(o.bet_type),
            "odds_data": {k: v for k, v in (o.odds_data or {}).items() if not str(k).startswith("_")},
            "spread": o.spread,
            "total": o.total,
            "provider": o.provider,
            "is_live": o.is_live,
            "valid_from": o.valid_from.isoformat() if o.valid_from else None,
            "valid_to": o.valid_to.isoformat() if o.valid_to else None,
            "is_current": o.valid_to is None,
        })
    return rows


def _pick_single(
    *,
    market: dict,
    best: dict[str, tuple[str, float]],
    prediction: str,
    confidence: float,
    min_odds: float = 1.1,
    max_odds: float | None = 10.0,
) -> Optional[dict]:
    """严格按模型方向主推；仅当该侧赔率在配置区间内时才返回。"""
    from app.ai.analysis_filters import odds_in_configured_range

    bt = _normalize_bet_type(market.get("bet_type") or "total")
    sels = tuple(market.get("selections") or ())
    if not best or not sels:
        return None
    candidates = [
        s
        for s in sels
        if s in best
        and odds_in_configured_range(
            best[s][1], min_odds=min_odds, max_odds=max_odds
        )
    ]
    if not candidates:
        return None

    pred = normalize_prediction(prediction, bet_type=bt)
    probs = _side_probs(pred, confidence, sels, bet_type=bt)
    if pred and pred in sels:
        if pred not in candidates:
            return None
        prov, odds = best[pred]
        win_prob = float(probs.get(pred, 0.0))
        return {
            "selection": pred,
            "selection_label": SEL_LABELS.get(pred, pred),
            "provider": prov,
            "provider_code": code_by_provider(prov) or "",
            "odds": float(odds),
            "win_rate": round(win_prob * 100, 1),
            "side_win_rates": {
                s: round(float(probs.get(s, 0.0)) * 100, 1) for s in sels if s in best
            },
            "pick_reason": "model_pick",
            "bet_type": bt,
        }

    # 未提供明确方向时，才允许按当前胜率最高的一侧回退
    scored: list[tuple[float, float, float, str]] = []
    for s in candidates:
        p = float(probs.get(s, 0.0))
        if pred and pred not in sels:
            p = min(p, max(0.45, float(confidence) * 0.85))
        o = float(best[s][1])
        scored.append((p, o, s))
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    win_prob, _, pick = scored[0]

    prov, odds = best[pick]
    return {
        "selection": pick,
        "selection_label": SEL_LABELS.get(pick, pick),
        "provider": prov,
        "provider_code": code_by_provider(prov) or "",
        "odds": float(odds),
        "win_rate": round(win_prob * 100, 1),
        "side_win_rates": {
            s: round(float(probs.get(s, 0.0)) * 100, 1) for s in sels if s in best
        },
        "pick_reason": "model_pick" if pred == pick else "highest_win_rate",
        "bet_type": bt,
    }


async def build_match_market_recommendations(
    db: AsyncSession,
    *,
    match_id: int,
    sport: str,
    prediction: str,
    confidence: float,
    stake: Decimal = Decimal("100"),
    providers_filter: set[str] | None = None,
    min_odds: float = 1.1,
    max_odds: float | None = 10.0,
    preferred_bet_type: str | None = None,
) -> list[dict]:
    """生成各盘口单边推荐；足球含胜负/让球/大小。"""
    from app.ai.analysis_filters import odds_in_configured_range

    _ = stake
    sport_key = _normalize_sport(sport)
    catalogs = SPORT_MARKETS.get(sport_key, SPORT_MARKETS["football"])
    filt = set(providers_filter or SINGLE_SIDE_NAMES) & SINGLE_SIDE_NAMES
    lo = float(min_odds)
    hi = float(max_odds) if max_odds is not None else None
    pref_bt = _normalize_bet_type(preferred_bet_type or "")
    pred = normalize_prediction(prediction, bet_type=pref_bt) or normalize_prediction(prediction)
    out: list[dict] = []

    for market in catalogs:
        bt = _normalize_bet_type(market["bet_type"])
        matrix, spread_line, total_line = await load_market_matrix(
            db, match_id, bt, providers_filter=filt or None
        )
        best_raw = best_by_selection(matrix)
        line = total_line if bt == "total" else (spread_line if bt == "spread" else None)
        sels = tuple(market.get("selections") or ())
        # 仅对模型选定盘口用满置信；其它盘口降权，避免三盘同时高胜率
        conf_use = float(confidence)
        if pref_bt and pref_bt != bt:
            conf_use = min(conf_use, 0.55)
        side_probs = _side_probs(pred if (not pref_bt or pref_bt == bt) else "", conf_use, sels, bet_type=bt)

        cells = []
        for sel in sels:
            if sel in best_raw:
                p, o = best_raw[sel]
                wr = round(float(side_probs.get(sel, 0.0)) * 100, 1)
                od = float(o)
                ok = odds_in_configured_range(od, min_odds=lo, max_odds=hi)
                cells.append({
                    "selection": sel,
                    "selection_label": SEL_LABELS.get(sel, sel),
                    "odds": od,
                    "provider": p,
                    "available": ok,
                    "win_rate": wr if ok else None,
                    "disabled_reason": None if ok else f"赔率不在 [{lo:g},{hi if hi is not None else '∞'}]",
                })
            else:
                cells.append({
                    "selection": sel,
                    "selection_label": SEL_LABELS.get(sel, sel),
                    "odds": None,
                    "provider": None,
                    "available": False,
                    "win_rate": None,
                })

        single = None
        if best_raw:
            single = _pick_single(
                market=market,
                best=best_raw,
                prediction=pred if (not pref_bt or pref_bt == bt) else "",
                confidence=conf_use,
                min_odds=lo,
                max_odds=hi,
            )
            # 非首选盘口：不产出可一键主推（避免误下）
            if single and pref_bt and pref_bt != bt:
                single = None

        move = await load_market_line_movement(
            db, match_id, bt, providers_filter=filt or None
        )
        opening_line = None
        if move and isinstance(move.get("opening"), dict):
            opening_line = move["opening"].get("line")

        out.append({
            "key": market["key"],
            "bet_type": bt,
            "label": market["label"],
            "line": line if line else None,
            "opening_line": opening_line,
            "line_movement": {
                "line_delta": move.get("line_delta"),
                "direction": move.get("direction"),
                "change_count": move.get("change_count"),
                "odds_delta": move.get("odds_delta"),
            } if move else None,
            "cells": cells,
            "available": bool(best_raw),
            "optional": False,
            "single": single,
            "highlight": [single["selection"]] if single else [],
        })

    return out


async def load_all_market_odds_pack(
    db: AsyncSession,
    match_id: int,
    sport: str,
    *,
    providers_filter: set[str] | None = None,
) -> dict[str, Any]:
    """供 LLM/启发式使用的多盘口赔率包（含亚洲盘初盘与盘口变化）。"""
    sport_key = _normalize_sport(sport)
    catalogs = SPORT_MARKETS.get(sport_key, SPORT_MARKETS["football"])
    markets: dict[str, Any] = {}
    flat: dict[str, float] = {}
    line_moves: dict[str, Any] = {}
    for market in catalogs:
        bt = _normalize_bet_type(market["bet_type"])
        matrix, spread_line, total_line = await load_market_matrix(
            db, match_id, bt, providers_filter=providers_filter
        )
        best = best_by_selection(matrix)
        if not best:
            continue
        odds = {sel: float(pair[1]) for sel, pair in best.items()}
        entry: dict[str, Any] = {"odds": odds}
        if bt == "total" and total_line:
            entry["line"] = float(total_line)
        if bt == "spread" and spread_line:
            entry["line"] = float(spread_line)
        move = await load_market_line_movement(
            db, match_id, bt, providers_filter=providers_filter
        )
        if move:
            entry["opening"] = move.get("opening")
            entry["line_movement"] = {
                "line_delta": move.get("line_delta"),
                "odds_delta": move.get("odds_delta"),
                "direction": move.get("direction"),
                "change_count": move.get("change_count"),
                "recent_moves": move.get("recent_moves"),
                "provider": move.get("provider"),
            }
            line_moves[bt] = move
        markets[bt] = entry
        for sel, od in odds.items():
            flat[sel] = float(od)
    return {"markets": markets, "flat": flat, "line_movements": line_moves, "odds_style": "asian"}
