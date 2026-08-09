"""端到端验证：预取 nowscore 基本面到 Redis + 触发 AI 大小球分析。

流程：
1. prefetch_today_all_contexts("football")  -> Redis/DB 写入基本面
2. 选一场有实时盘口的比赛 -> analyze_and_recommend(match_id, user_id)
3. 打印 AI 大小球走向结果
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("run_ai_analysis")


async def main():
    # ── Step 0: 连接 Redis cache ──
    from app.core.cache import cache
    await cache.connect()

    # ── Step 1: 预取今日全量足球基本面到 Redis ──
    from app.services.nowscore_scraper import prefetch_today_all_contexts

    logger.info("=== Step 1: 预取 nowscore 足球基本面 ===")
    fb_count = await prefetch_today_all_contexts(sport="football", concurrency=8)
    logger.info("足球基本面缓存比赛数: %d", fb_count)

    # ── Step 2: 选比赛并触发 AI 大小球分析 ──
    # match 1019: 德文波特城前锋 vs 朗赛斯顿城 (2-0 上半场, total line ~3.5)
    # match 1016: 阿尔弗斯通SC vs 东南联 (1-0 上半场, total line 3.5/3.75)
    match_id = int(sys.argv[1]) if len(sys.argv) > 1 else 1019
    user_id = 3  # ai_trader

    logger.info("=== Step 2: 检查 Redis 基本面是否命中 match=%s ===", match_id)
    from app.database import AsyncSessionLocal
    from app.models.user import Match as MatchModel
    from app.services.fixture_key import fixture_key_for_match
    from app.services.match_context_store import load_from_redis
    from sqlalchemy import select

    async with AsyncSessionLocal() as db:
        match = (await db.execute(select(MatchModel).where(MatchModel.id == match_id))).scalar_one_or_none()
        await db.commit()
        if not match:
            logger.error("比赛 %s 不存在", match_id)
            return
        fk = fixture_key_for_match(match)
        logger.info("比赛: %s vs %s | sport=%s | fixture_key=%s", match.home_team, match.away_team, match.sport, fk)

    redis_ctx = await load_from_redis(fk)
    if redis_ctx:
        logger.info(
            "Redis 基本面命中: source=%s present=%s completeness=%s",
            redis_ctx.get("source"),
            redis_ctx.get("dimensions_present"),
            (redis_ctx.get("quality") or {}).get("completeness"),
        )
        # 打印基本面关键数据摘要
        _print_fundamentals(redis_ctx)
    else:
        logger.warning("Redis 未命中基本面，将触发 nowscore 实时抓取")

    # ── Step 3: 触发 AI 大小球分析（实时盘口 + 基本面）──
    logger.info("=== Step 3: 触发 AI 大小球分析 match=%s user=%s ===", match_id, user_id)
    from app.ai.auto_better import analyze_and_recommend

    result = await analyze_and_recommend(match_id=match_id, user_id=user_id)

    logger.info("=== AI 分析结果 ===")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


def _print_fundamentals(ctx: dict):
    """打印基本面关键数据摘要。"""
    def _rows_summary(rows):
        if not rows:
            return "无"
        out = []
        for r in rows[:3]:
            if isinstance(r, dict):
                out.append({"header": r.get("header"), "rows_n": len(r.get("rows") or [])})
        return out

    print("\n--- 基本面数据摘要 ---")
    print(f"交锋(H2H): {len((ctx.get('h2h') or {}).get('matches') or [])} 场")
    print(f"主队近况: {len((ctx.get('home_form') or {}).get('matches') or [])} 场")
    print(f"客队近况: {len((ctx.get('away_form') or {}).get('matches') or [])} 场")
    st = ctx.get("standings") or {}
    print(f"积分排名: home={bool(st.get('home'))} away={bool(st.get('away'))}")
    ana = ctx.get("analysis") or {}
    print(f"伤停: {_rows_summary(ana.get('injuries'))}")
    print(f"战绩特征: {_rows_summary(ana.get('features'))}")
    print(f"数据对比: {_rows_summary(ana.get('compare'))}")
    live = ctx.get("live") or {}
    print(f"首发阵容: home={bool(live.get('lineup', {}).get('home'))} away={bool(live.get('lineup', {}).get('away'))}")
    print(f"进失球概率: {bool(live.get('goal_probability'))}")
    print(f"半场/全场统计: {bool(live.get('half_full_stats'))}")
    tr = ctx.get("trend") or {}
    print(f"各公司初指(赛前指数): {len(tr.get('initial_odds') or [])} 表")
    print("----------------------\n")


if __name__ == "__main__":
    asyncio.run(main())
