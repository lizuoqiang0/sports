"""AI 分析下单测试：一单 10 元"""
import asyncio
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import AsyncSessionLocal
from app.models import Odds, Match, User, Bet
from app.ai.strategy import StrategyEngine, StrategyConfig, SPORT_RISK
from sqlalchemy import select, and_

async def main():
    async with AsyncSessionLocal() as db:
        # 找一场有 under 赔率的平博滚球比赛
        q = (
            select(Match, Odds)
            .join(Odds, and_(
                Odds.match_id == Match.id,
                Odds.bet_type == "total",
                Odds.provider_code == "pinnacle",
            ))
            .where(Match.status == "live")
            .order_by(Match.id.desc())
            .limit(5)
        )
        res = await db.execute(q)
        rows = res.all()

        if not rows:
            print("❌ 没有找到符合条件的比赛")
            return

        print(f"找到 {len(rows)} 场候选比赛:\n")
        for m, o in rows:
            odds_data = o.odds_json or {}
            under_odds = odds_data.get("under")
            total_line = o.total_line
            print(f"  match_id={m.id} {m.home_team} vs {m.away_team}")
            print(f"    league={m.league} line={total_line} under={under_odds}")
            print(f"    period={m.period} clock={m.clock} score={m.home_score}-{m.away_score}")

        # 取第一场，构造 under 分析
        m, o = rows[0]
        odds_data = o.odds_json or {}
        under_odds = odds_data.get("under", 1.90)
        total_line = float(o.total_line or 2.5)

        print(f"\n{'='*60}")
        print(f"选中: {m.home_team} vs {m.away_team}")
        print(f"line={total_line} under={under_odds}")
        print(f"{'='*60}")

        # 策略引擎（放宽门槛）
        config = StrategyConfig(
            name="test_bet",
            max_bet_amount=10.0,
            max_daily_bets=10,
            stop_loss=100.0,
            take_profit=200.0,
            use_llm_analysis=True,
            min_confidence=0.0,
            min_odds=1.01,
            max_odds=99.0,
        )
        engine = StrategyEngine(config, user_id=1)

        # 构造分析结果
        analysis = {
            "prediction": "under",
            "confidence": 0.65,
            "odds": float(under_odds),
            "consensus_reached": True,
            "reasoning": "测试下单：AI分析小球",
            "line": total_line,
            "context_source": "api",
            "provider_code": "pinnacle",
        }

        match_info = {
            "id": m.id,
            "home_team": m.home_team,
            "away_team": m.away_team,
            "sport": m.sport or "football",
            "odds": {"under": under_odds},
            "provider_code": "pinnacle",
            "league": m.league or "",
            "period": m.period or "",
            "clock": m.clock or "",
            "home_score": m.home_score or 0,
            "away_score": m.away_score or 0,
        }

        # 获取用户余额
        user = await db.get(User, 1)
        balance = Decimal(str(user.balance if user and user.balance else 100))

        print(f"\n用户余额: {balance}")
        print(f"评估中...")

        decision = await engine.evaluate_bet(
            match_info=match_info,
            analysis=analysis,
            user_balance=balance,
            daily_loss=Decimal("0"),
            active_bets_count=0,
        )

        print(f"\nshould_bet: {decision.should_bet}")
        if decision.should_bet:
            print(f"selection: {decision.selection}")
            print(f"suggested_stake: {decision.suggested_stake}")
            print(f"bet_type: {decision.bet_type}")
            print(f"odds: {decision.odds}")
        print(f"reasoning: {decision.reasoning[:200]}")

        if not decision.should_bet:
            print(f"\n❌ 策略拒绝")
            return

        print(f"\n✅ 策略通过，准备下单...")
        # 实际下单需要通过 API 或 auto_better._execute_bet
        print(f"下单参数: match_id={m.id}, bet_type={decision.bet_type}, "
              f"sel={decision.selection}, stake={decision.suggested_stake}, odds={decision.odds}")

if __name__ == "__main__":
    asyncio.run(main())
