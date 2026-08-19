#!/usr/bin/env python3
"""OB-Sports 实时监控脚本：比分/时钟/赔率/策略闸门/AI 引擎状态。

日志输出到 stdout + /tmp/live_monitor.log，同时通过 WebSocket 推送到前端 AI 日志面板。

用法：
    docker exec -d ob-backend bash -c "cd /app && PYTHONPATH=/app python3 scripts/live_monitor.py"
    docker exec ob-backend tail -f /tmp/live_monitor.log
"""
import asyncio
import json
import logging
import sys
import time
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("/tmp/live_monitor.log", mode="a"),
    ],
)
log = logging.getLogger("live_monitor")

from app.database import AsyncSessionLocal
from sqlalchemy import text
from app.ai.strategy import SPORT_RISK, LEAGUE_BLACKLIST_KEYWORDS, league_is_blacklisted
from app.ai.analyzer import MatchAnalyzer


async def _broadcast_ai_log(payload: dict):
    """通过 WebSocket 推送监控结果到 AI 日志频道。"""
    try:
        from app.core.websocket import manager

        # 广播到 ai_logs 频道（前端订阅）
        await manager.broadcast_to_channel("ai_logs", {
            "type": "ai_monitor",
            "data": payload,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        # 同时广播到所有用户（确保不遗漏）
        await manager.broadcast_all({
            "type": "ai_monitor",
            "data": payload,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        log.debug(f"WebSocket 广播失败（非致命）: {e}")


async def check_matches(db):
    """检查 LIVE 比赛数据质量。"""
    r = await db.execute(text("""
        SELECT m.id, m.home_team, m.away_team, m.home_score, m.away_score,
               m.extra_data->>'clock' as clock,
               m.extra_data->>'period' as period,
               m.sport::text, m.league,
               m.extra_data->>'site_code' as site,
               (SELECT count(*) FROM odds o WHERE o.match_id=m.id AND o.is_live=true) as odds_count
        FROM matches m
        WHERE m.status='LIVE' AND m.sport IN ('FOOTBALL','BASKETBALL')
        ORDER BY m.extra_data->>'site_code', m.sport, m.id
    """))
    rows = r.fetchall()
    issues = []

    for row in rows:
        mid, home, away, hs, aws, clock, period, sport, league, site, oc = row
        sport_lower = sport.lower() if sport else ""
        tag = f"[{site or '?'}] {sport_lower}"

        # 1. 比分检查
        if hs is None or aws is None:
            issues.append({"level": "error", "tag": tag, "match": f"{home} vs {away}", "issue": "比分缺失"})
        elif sport_lower == "football" and (hs > 10 or aws > 10):
            issues.append({"level": "error", "tag": tag, "match": f"{home} vs {away}", "issue": f"比分异常 {hs}:{aws}（足球>10）"})
        elif sport_lower == "basketball" and (hs < 5 or aws < 5):
            issues.append({"level": "warn", "tag": tag, "match": f"{home} vs {away}", "issue": f"比分偏低 {hs}:{aws}（篮球<5）"})

        # 2. 时钟检查
        if not clock or clock.strip() == "":
            issues.append({"level": "warn", "tag": tag, "match": f"{home} vs {away}", "issue": "时钟为空"})

        # 3. 赔率检查
        if oc == 0:
            issues.append({"level": "error", "tag": tag, "match": f"{home} vs {away}", "issue": "无赔率数据"})
        elif oc < 3:
            issues.append({"level": "warn", "tag": tag, "match": f"{home} vs {away}", "issue": f"赔率偏少 ({oc})"})

        # 4. 联赛黑名单检查
        if league and league_is_blacklisted(league):
            issues.append({"level": "warn", "tag": tag, "match": f"{home} vs {away}", "issue": f"联赛黑名单: {league}"})

    return rows, issues


async def check_odds_quality(db):
    """检查赔率结构完整性（TOTAL over / SPREAD handicap）。"""
    r = await db.execute(text("""
        SELECT DISTINCT ON (m.id, o.bet_type)
               m.home_team, m.away_team, o.bet_type::text,
               o.odds_data->>'under' as under_odds,
               o.odds_data->>'over' as over_odds,
               o.odds_data->>'handicap' as handicap,
               o.total, o.spread,
               m.extra_data->>'site_code' as site
        FROM odds o JOIN matches m ON o.match_id=m.id
        WHERE o.is_live=true AND m.status='LIVE'
        ORDER BY m.id, o.bet_type, o.valid_from DESC
        LIMIT 50
    """))
    rows = r.fetchall()
    issues = []

    for row in rows:
        home, away, bt, under, over, hd, tot, sp, site = row
        tag = f"[{site or '?'}] {bt}"
        match = f"{home} vs {away}"

        # TOTAL: 检查 under/over/total
        if bt == "TOTAL":
            if not under and not over:
                issues.append({"level": "error", "tag": tag, "match": match, "issue": "TOTAL 无 under/over"})
            elif not under:
                issues.append({"level": "warn", "tag": tag, "match": match, "issue": "TOTAL 缺 under"})
            elif not over:
                issues.append({"level": "warn", "tag": tag, "match": match, "issue": "TOTAL 缺 over"})
            if not tot:
                issues.append({"level": "warn", "tag": tag, "match": match, "issue": "TOTAL 无盘口线"})

        # SPREAD: 检查 handicap/spread
        if bt == "SPREAD":
            if not hd and not sp:
                issues.append({"level": "warn", "tag": tag, "match": match, "issue": "SPREAD 无让球线"})

        # 赔率值范围检查
        for label, val in [("under", under), ("over", over)]:
            if val:
                try:
                    fval = float(val)
                    if fval < 1.01 or fval > 100:
                        issues.append({"level": "error", "tag": tag, "match": match, "issue": f"赔率异常 {label}={val}"})
                except (ValueError, TypeError):
                    issues.append({"level": "error", "tag": tag, "match": match, "issue": f"赔率非数字 {label}={val}"})

    return rows, issues


async def check_ai_engine_status():
    """检查 AI 引擎运行状态和 Redis 锁。"""
    from app.core.cache import cache
    issues = []
    engine_status = {"running": False, "lock": None}

    try:
        running = await cache.get("ai:engine:running:1")
        lock = await cache.get("ai:engine:lock:1")
        engine_status = {
            "running": bool(running),
            "lock": str(lock)[:12] if lock else None,
        }
        log.info(f"  AI 引擎: {'运行中' if running else '未运行'} | {'锁持有(' + str(lock)[:8] + '...)' if lock else '无锁'}")
    except Exception as e:
        issues.append({"level": "error", "tag": "[redis]", "match": "", "issue": f"Redis 状态检查失败: {e}"})

    return issues, engine_status


async def check_strategy_gates():
    """检查策略闸门配置一致性。"""
    issues = []
    import inspect
    from app.ai.strategy import StrategyEngine

    # SPORT_RISK 参数
    bk = SPORT_RISK.get("basketball", {})
    fb = SPORT_RISK.get("football", {})
    if "under_min_line" not in bk:
        issues.append({"level": "error", "tag": "[SPORT_RISK]", "match": "", "issue": "篮球缺 under_min_line"})
    if bk.get("under_min_line") != 120.0:
        issues.append({"level": "warn", "tag": "[SPORT_RISK]", "match": "", "issue": f"篮球 under_min_line={bk.get('under_min_line')} (应为120)"})
    if fb.get("under_max_line") != 6.5:
        issues.append({"level": "warn", "tag": "[SPORT_RISK]", "match": "", "issue": f"足球 under_max_line={fb.get('under_max_line')} (应为6.5)"})

    # 黑名单
    if "友谊赛" not in LEAGUE_BLACKLIST_KEYWORDS:
        issues.append({"level": "error", "tag": "[黑名单]", "match": "", "issue": "缺'友谊赛'"})
    if "表演赛" not in LEAGUE_BLACKLIST_KEYWORDS:
        issues.append({"level": "error", "tag": "[黑名单]", "match": "", "issue": "缺'表演赛'"})

    # A2 闸门检查
    src = inspect.getsource(StrategyEngine.evaluate_bet)
    if "if False" in src and "TEMP: bypass" in src:
        issues.append({"level": "error", "tag": "[A2闸门]", "match": "", "issue": "仍被 if False 旁路!"})

    # _elapsed_minutes 检查
    if not hasattr(MatchAnalyzer, "_elapsed_minutes"):
        issues.append({"level": "error", "tag": "[Analyzer]", "match": "", "issue": "缺 _elapsed_minutes 方法"})

    # GPT 重试次数
    gpt_src = inspect.getsource(MatchAnalyzer._call_gpt)
    if "max_retries = 3" in gpt_src:
        issues.append({"level": "warn", "tag": "[GPT]", "match": "", "issue": "重试仍为 3（应为2）"})

    # 缓存 TTL
    analyze_src = inspect.getsource(MatchAnalyzer.analyze_match)
    if "ttl=180" not in analyze_src and "180" not in analyze_src:
        issues.append({"level": "warn", "tag": "[缓存]", "match": "", "issue": "正缓存 TTL 未缩短到 180s"})

    return issues


async def monitor_cycle():
    """单轮监控。"""
    now = datetime.now(timezone.utc)
    now_str = now.strftime("%Y-%m-%d %H:%M:%S UTC")
    log.info(f"{'='*60}")
    log.info(f"📊 实时监控 | {now_str}")
    log.info(f"{'='*60}")

    monitor_data = {
        "timestamp": now.isoformat(),
        "matches": {"total": 0, "ob": 0, "pinnacle": 0},
        "odds": {"total": 0, "total_complete": 0, "spread_complete": 0},
        "engine": {"running": False, "lock": None},
        "issues": [],
        "summary": "",
    }

    async with AsyncSessionLocal() as db:
        # 1. 比赛数据
        matches, match_issues = await check_matches(db)
        ob_count = sum(1 for r in matches if r[9] == "ob")
        pin_count = sum(1 for r in matches if r[9] == "pinnacle")
        monitor_data["matches"] = {"total": len(matches), "ob": ob_count, "pinnacle": pin_count}

        log.info(f"✅ LIVE 比赛: {len(matches)} 场 | OB: {ob_count} | 平博: {pin_count}")

        if match_issues:
            log.warning(f"⚠️  比赛数据问题 ({len(match_issues)}):")
            for issue in match_issues[:10]:
                log.warning(f"  {issue['level']:<5} {issue['tag']} {issue['match']} | {issue['issue']}")
            if len(match_issues) > 10:
                log.warning(f"   ... 还有 {len(match_issues)-10} 条")
        else:
            log.info("✅ 比赛数据正常")

        # 2. 赔率质量
        odds, odds_issues = await check_odds_quality(db)
        total_count = len(odds)
        total_ok = sum(1 for r in odds if r[3] and r[4])  # under + over 都有
        spread_ok = sum(1 for r in odds if r[7] or r[6])  # spread 或 handicap 有
        monitor_data["odds"] = {
            "total": total_count,
            "total_complete": total_ok,
            "spread_complete": spread_ok,
        }

        log.info(f"✅ 赔率: {total_count} 条 | TOTAL 齐全: {total_ok} | SPREAD 有让球线: {spread_ok}")

        if odds_issues:
            log.warning(f"⚠️  赔率问题 ({len(odds_issues)}):")
            for issue in odds_issues[:10]:
                log.warning(f"  {issue['level']:<5} {issue['tag']} {issue['match']} | {issue['issue']}")
        else:
            log.info("✅ 赔率结构正常")

    # 3. AI 引擎状态
    engine_issues, engine_status = await check_ai_engine_status()
    monitor_data["engine"] = engine_status

    # 4. 策略闸门
    gate_issues = await check_strategy_gates()
    if gate_issues:
        log.warning(f"⚠️  策略闸门问题 ({len(gate_issues)}):")
        for issue in gate_issues:
            log.warning(f"  {issue['level']:<5} {issue['tag']} {issue['issue']}")
    else:
        log.info("✅ 策略闸门配置正常")

    # 汇总
    all_issues = match_issues + odds_issues + engine_issues + gate_issues
    monitor_data["issues"] = all_issues
    error_count = sum(1 for i in all_issues if i.get("level") == "error")
    warn_count = sum(1 for i in all_issues if i.get("level") == "warn")

    if not all_issues:
        summary = f"全部正常 | 比赛={len(matches)} 赔率={total_count} 问题=0"
        log.info(f"🎉 {summary}")
    else:
        summary = f"共 {len(all_issues)} 个问题 ({error_count} 错误 / {warn_count} 警告)"
        log.warning(f"⚠️  {summary}")

    monitor_data["summary"] = summary
    log.info("")

    # 通过 WebSocket 推送到前端 AI 日志面板
    await _broadcast_ai_log(monitor_data)


async def main():
    """主循环：每 30 秒一轮。"""
    log.info("🚀 OB-Sports 实时监控启动 (间隔=30s, 日志+WebSocket)")
    while True:
        try:
            await monitor_cycle()
        except Exception as e:
            log.error(f"监控异常: {e}", exc_info=True)
        await asyncio.sleep(30)


if __name__ == "__main__":
    asyncio.run(main())
