"""只读回放70%–80%平衡档边界；不修改数据库、不触发下单。"""
from __future__ import annotations

import asyncio
import os
import sys
from collections import defaultdict

from sqlalchemy import select

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.ai.balanced_profile import balanced_auto_eligible
from app.ai.league_focus import league_focus_level
from app.database import AsyncSessionLocal
from app.models.user import Bet, Match


async def main() -> None:
    async with AsyncSessionLocal() as db:
        rows = (
            await db.execute(
                select(Bet, Match)
                .join(Match, Bet.match_id == Match.id)
                .where(Bet.settled_at.is_not(None), Bet.is_ai_bet.is_(True))
                .order_by(Bet.created_at)
            )
        ).all()

    buckets = defaultdict(lambda: {"n": 0, "won": 0, "pnl": 0.0})
    for bet, match in rows:
        sport = str(match.sport.value if hasattr(match.sport, "value") else match.sport).lower()
        elapsed = (bet.created_at - match.start_time).total_seconds() / 60.0
        ok, _ = balanced_auto_eligible(
            sport=sport,
            league=str(match.league or ""),
            selection=str(bet.selection or ""),
            confidence=float(bet.ai_confidence or 0),
            line=float(bet.line) if bet.line is not None else None,
            odds=float(bet.odds or 0),
            played_minutes=elapsed,
        )
        if not ok:
            continue
        stake = float(bet.stake or 0)
        payout = float(bet.actual_payout or 0)
        if abs(payout - stake) <= 1e-9:
            continue
        key = (sport, str(bet.selection or "").lower(), league_focus_level(sport, match.league or ""))
        bucket = buckets[key]
        bucket["n"] += 1
        bucket["won"] += int(payout > stake)
        bucket["pnl"] += payout - stake

    total_n = total_won = 0
    total_pnl = 0.0
    for (sport, selection, focus), data in sorted(buckets.items()):
        total_n += data["n"]
        total_won += data["won"]
        total_pnl += data["pnl"]
        print(
            f"sport={sport} selection={selection} focus={focus} n={data['n']} "
            f"wins={data['won']} win_rate={data['won']/data['n']:.1%} pnl={data['pnl']:.2f}"
        )
    print(
        f"balanced_total={total_n} wins={total_won} "
        f"win_rate={(total_won/total_n if total_n else 0):.1%} pnl={total_pnl:.2f}"
    )


if __name__ == "__main__":
    asyncio.run(main())
