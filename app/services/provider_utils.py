"""站点工具：provider 名称/代码映射与赔率矩阵。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import BetType, Odds
from app.services.bookmakers.catalog import BOOKMAKER_CATALOG


def code_by_provider(name: str) -> Optional[str]:
    for code, meta in BOOKMAKER_CATALOG.items():
        if meta.get("name") == name:
            return code
    alias = {
        "OB Sports": "ob",
        "OB": "ob",
        "OB体育": "ob",
        "Pinnacle": "pinnacle",
        "平博": "pinnacle",
    }
    return alias.get(str(name or "").strip())


# 兼容旧调用名
_code_by_provider = code_by_provider


async def load_odds_matrix(
    db: AsyncSession,
    match_id: int,
    *,
    bet_type: BetType = BetType.TOTAL,
) -> dict[str, dict[str, float]]:
    """返回 {selection: {provider: odds}}，仅当前有效赔率。"""
    result = await db.execute(
        select(Odds).where(
            and_(
                Odds.match_id == match_id,
                Odds.bet_type == bet_type,
                Odds.valid_to.is_(None),
                func.coalesce(Odds.last_seen_at, Odds.valid_from) >= (
                    datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5)
                ),
            )
        )
    )
    matrix: dict[str, dict[str, float]] = {}
    for row in result.scalars().all():
        for sel, val in (row.odds_data or {}).items():
            if str(sel).startswith("_"):
                continue
            try:
                f = float(val)
            except (TypeError, ValueError):
                continue
            if f > 1.0:
                matrix.setdefault(str(sel), {})[row.provider] = f
    return matrix


def best_by_selection(matrix: dict[str, dict[str, float]]) -> dict[str, tuple[str, float]]:
    """selection -> (provider, odds)"""
    best = {}
    for sel, providers in matrix.items():
        if not providers:
            continue
        p, o = max(providers.items(), key=lambda x: x[1])
        best[sel] = (p, o)
    return best


_best_by_selection = best_by_selection


async def compare_match_odds(db: AsyncSession, match_id: int) -> dict:
    """OB / 平博全场大小球比价。"""
    matrix = await load_odds_matrix(db, match_id, bet_type=BetType.TOTAL)
    # 仅保留单边站点的全场大小球方向；让球/胜负不进入比价结果。
    allow = {"OB体育", "平博"}
    filtered: dict[str, dict[str, float]] = {}
    for sel, provs in matrix.items():
        if str(sel).lower() not in {"under", "over"}:
            continue
        for p, o in (provs or {}).items():
            if p in allow:
                filtered.setdefault(sel, {})[p] = o
    best = best_by_selection(filtered)
    return {
        "match_id": match_id,
        "bet_type": "total",
        "matrix": filtered,
        "best": {k: {"provider": v[0], "odds": v[1]} for k, v in best.items()},
    }


def site_code_from_match(match) -> str:
    """从 external_id / extra_data 推断站点代码。"""
    ext = str(getattr(match, "external_id", "") or "")
    if ":" in ext:
        return ext.split(":", 1)[0].lower()
    extra = getattr(match, "extra_data", None) or {}
    if isinstance(extra, dict):
        ids = extra.get("ids") or {}
        if isinstance(ids, dict):
            for code in ("ob", "pinnacle"):
                if ids.get(code):
                    return code
        src = str(extra.get("source") or "")
        for code in ("ob", "pinnacle"):
            if src.startswith(f"{code}_"):
                return code
    return ""
