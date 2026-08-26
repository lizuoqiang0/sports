"""只读回测高精度闸门；不会修改数据库或触发下单。"""
from __future__ import annotations

import asyncio
import math
import os
import sys

from sqlalchemy import select

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.ai.precision_profile import high_precision_history_eligible
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

    selected = []
    for bet, match in rows:
        elapsed = (bet.created_at - match.start_time).total_seconds() / 60.0
        sport = str(match.sport.value if hasattr(match.sport, "value") else match.sport)
        if not high_precision_history_eligible(
            sport=sport,
            selection=bet.selection,
            confidence=float(bet.ai_confidence or 0),
            line=float(bet.line) if bet.line is not None else None,
            odds=float(bet.odds or 0),
            played_minutes=elapsed,
        ):
            continue
        stake = float(bet.stake or 0)
        payout = float(bet.actual_payout or 0)
        if abs(payout - stake) <= 1e-9:
            continue
        selected.append((bet, payout > stake, payout - stake))

    wins = sum(1 for _, won, _ in selected if won)
    n = len(selected)
    pnl = sum(pnl for _, _, pnl in selected)
    rate = wins / n if n else 0.0
    if n:
        z = 1.96
        denom = 1 + z * z / n
        center = (rate + z * z / (2 * n)) / denom
        radius = z * math.sqrt(rate * (1 - rate) / n + z * z / (4 * n * n)) / denom
        interval = f"[{max(0.0, center-radius):.1%},{min(1.0, center+radius):.1%}]"
    else:
        interval = "n/a"
    print(
        f"eligible={n} wins={wins} win_rate={rate:.1%} pnl={pnl:.2f} "
        f"wilson95={interval}"
    )


if __name__ == "__main__":
    asyncio.run(main())
