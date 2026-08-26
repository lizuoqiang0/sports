"""实时监控后台服务：比分/时钟/赔率/策略闸门/AI 引擎状态。

作为 app 内后台任务运行（由 main.py 的 supervisor 跟随 leader 启停），
与 WebSocket 连接同进程，监控结果实时推送到前端 AI 日志面板（ai_logs 频道）。
"""
import asyncio
import inspect
import logging
from datetime import datetime, timezone

from app.database import AsyncSessionLocal
from sqlalchemy import select, text

logger = logging.getLogger("app.services.live_monitor")

# 监控开关与间隔（秒）
_MONITOR_ENABLED = True
_MONITOR_INTERVAL = 30.0

_task: asyncio.Task | None = None


async def _broadcast_ai_log(payload: dict):
    """通过 WebSocket 推送监控结果到前端 AI 日志面板。"""
    try:
        from app.core.websocket import manager

        await manager.broadcast_all({
            "type": "ai_monitor",
            "data": payload,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as e:
        logger.debug(f"WebSocket 广播失败（非致命）: {e}")


async def _check_matches(db):
    """检查 LIVE 比赛数据质量。"""
    from app.ai.strategy import league_is_blacklisted

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
        _mid, home, away, hs, aws, clock, _period, sport, league, _site, oc = row
        sport_lower = (sport or "").lower()
        tag = f"[{_site or '?'}] {sport_lower}"
        match_name = f"{home} vs {away}"

        if hs is None or aws is None:
            issues.append({"level": "error", "tag": tag, "match": match_name, "issue": "比分缺失"})
        elif sport_lower == "football" and (hs > 10 or aws > 10):
            issues.append({"level": "error", "tag": tag, "match": match_name, "issue": f"比分异常 {hs}:{aws}（足球>10）"})
        elif sport_lower == "basketball" and (hs < 5 or aws < 5):
            issues.append({"level": "warn", "tag": tag, "match": match_name, "issue": f"比分偏低 {hs}:{aws}（篮球<5）"})

        if not clock or clock.strip() == "":
            issues.append({"level": "warn", "tag": tag, "match": match_name, "issue": "时钟为空"})

        if oc == 0:
            issues.append({"level": "error", "tag": tag, "match": match_name, "issue": "无赔率数据"})
        elif oc < 3:
            issues.append({"level": "warn", "tag": tag, "match": match_name, "issue": f"赔率偏少 ({oc})"})

        if league and league_is_blacklisted(league):
            issues.append({"level": "warn", "tag": tag, "match": match_name, "issue": f"联赛黑名单: {league}"})

    return rows, issues


async def _check_odds_quality(db):
    """检查赔率结构完整性（TOTAL under/over / SPREAD 让球线）。"""
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
        match_name = f"{home} vs {away}"

        if bt == "TOTAL":
            if not under and not over:
                issues.append({"level": "error", "tag": tag, "match": match_name, "issue": "TOTAL 无 under/over"})
            elif not under:
                issues.append({"level": "warn", "tag": tag, "match": match_name, "issue": "TOTAL 缺 under"})
            elif not over:
                issues.append({"level": "warn", "tag": tag, "match": match_name, "issue": "TOTAL 缺 over"})
            if not tot:
                issues.append({"level": "warn", "tag": tag, "match": match_name, "issue": "TOTAL 无盘口线"})

        if bt == "SPREAD" and not hd and not sp:
            issues.append({"level": "warn", "tag": tag, "match": match_name, "issue": "SPREAD 无让球线"})

        for label, val in [("under", under), ("over", over)]:
            if val:
                try:
                    fval = float(val)
                    if fval < 1.01 or fval > 100:
                        issues.append({"level": "error", "tag": tag, "match": match_name, "issue": f"赔率异常 {label}={val}"})
                except (ValueError, TypeError):
                    issues.append({"level": "error", "tag": tag, "match": match_name, "issue": f"赔率非数字 {label}={val}"})

    return rows, issues


async def _check_ai_engine_status(user_id: int | None = None):
    """检查指定用户的 AI 引擎运行状态和 Redis 锁。"""
    from app.core.cache import cache

    issues = []
    engine_status = {"running": False, "lock": None}
    try:
        uid = int(user_id or 0)
        running = await cache.get(f"ai:engine:running:{uid}") if uid else None
        lock = await cache.get(f"ai:engine:lock:{uid}") if uid else None
        engine_status = {
            "running": bool(running),
            "lock": str(lock)[:12] if lock else None,
        }
    except Exception as e:
        issues.append({"level": "error", "tag": "[redis]", "match": "", "issue": f"Redis 状态检查失败: {e}"})
    return issues, engine_status


async def _check_strategy_gates():
    """检查策略闸门配置一致性。"""
    from app.ai.analyzer import MatchAnalyzer
    from app.ai.strategy import LEAGUE_BLACKLIST_KEYWORDS, StrategyEngine

    issues = []

    if "友谊赛" not in LEAGUE_BLACKLIST_KEYWORDS:
        issues.append({"level": "error", "tag": "[黑名单]", "match": "", "issue": "缺'友谊赛'"})
    if "表演赛" not in LEAGUE_BLACKLIST_KEYWORDS:
        issues.append({"level": "error", "tag": "[黑名单]", "match": "", "issue": "缺'表演赛'"})

    try:
        src = inspect.getsource(StrategyEngine.evaluate_bet)
        if "if False" in src and "TEMP: bypass" in src:
            issues.append({"level": "error", "tag": "[A2闸门]", "match": "", "issue": "仍被 if False 旁路!"})
        if "evaluate_balanced_gate" not in src:
            issues.append({"level": "error", "tag": "[组合闸门]", "match": "", "issue": "生产策略缺 evaluate_balanced_gate"})
    except (OSError, TypeError):
        pass

    if not hasattr(MatchAnalyzer, "_elapsed_minutes"):
        issues.append({"level": "error", "tag": "[Analyzer]", "match": "", "issue": "缺 _elapsed_minutes 方法"})

    return issues


async def monitor_cycle():
    """单轮监控：检查 + 日志 + WebSocket 推送。"""
    now = datetime.now(timezone.utc)
    monitor_data = {
        "timestamp": now.isoformat(),
        "matches": {"total": 0, "ob": 0, "pinnacle": 0},
        "odds": {"total": 0, "total_complete": 0, "spread_complete": 0},
        "engine": {"running": False, "lock": None},
        "issues": [],
        "summary": "",
    }

    async with AsyncSessionLocal() as db:
        matches, match_issues = await _check_matches(db)
        ob_count = sum(1 for r in matches if r[9] == "ob")
        pin_count = sum(1 for r in matches if r[9] == "pinnacle")
        monitor_data["matches"] = {"total": len(matches), "ob": ob_count, "pinnacle": pin_count}

        odds, odds_issues = await _check_odds_quality(db)
        total_ok = sum(1 for r in odds if r[3] and r[4])
        spread_ok = sum(1 for r in odds if r[7] or r[6])
        monitor_data["odds"] = {"total": len(odds), "total_complete": total_ok, "spread_complete": spread_ok}

    # 监控必须读取实际启用 AI 的用户；固定查 user=1 会把 user=6 的运行状态
    # 永久误报为“未运行”，掩盖引擎停机/锁竞争问题。
    from app.models.user import User
    async with AsyncSessionLocal() as db:
        active_user_id = (
            await db.execute(
                select(User.id)
                .where(User.ai_enabled == True)  # noqa: E712
                .order_by(User.id)
                .limit(1)
            )
        ).scalar_one_or_none()
    engine_issues, engine_status = await _check_ai_engine_status(active_user_id)
    monitor_data["engine"] = engine_status

    gate_issues = await _check_strategy_gates()

    all_issues = match_issues + odds_issues + engine_issues + gate_issues
    monitor_data["issues"] = all_issues
    error_count = sum(1 for i in all_issues if i.get("level") == "error")
    warn_count = sum(1 for i in all_issues if i.get("level") == "warn")

    if not all_issues:
        summary = f"全部正常 | 比赛={len(matches)} 赔率={len(odds)} 问题=0"
    else:
        summary = f"共 {len(all_issues)} 个问题 ({error_count} 错误 / {warn_count} 警告)"
    monitor_data["summary"] = summary

    logger.info(
        "[实时监控] 比赛=%d (OB:%d/平博:%d) 赔率=%d TOTAL齐全=%d | 引擎=%s | %s",
        len(matches), ob_count, pin_count, len(odds), total_ok,
        "运行中" if engine_status.get("running") else "未运行",
        summary,
    )

    await _broadcast_ai_log(monitor_data)


async def _run_loop():
    logger.info("实时监控服务启动 (间隔=%.0fs)", _MONITOR_INTERVAL)
    while True:
        try:
            await monitor_cycle()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("实时监控单轮异常")
        await asyncio.sleep(_MONITOR_INTERVAL)


def start_live_monitor() -> None:
    """启动实时监控后台任务（幂等）。"""
    global _task
    if _task is not None and not _task.done():
        return
    _task = asyncio.create_task(_run_loop(), name="ob-live-monitor")


def stop_live_monitor() -> None:
    """停止实时监控后台任务。"""
    global _task
    if _task is not None:
        _task.cancel()
        _task = None
        logger.info("实时监控服务已停止")
