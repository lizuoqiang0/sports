"""
AI 自动投注引擎 - 主调度器

工作流程:
1. 定时扫描 OB / 平博 各自滚球（足球/篮球）赛事
2. 用 LLM + 盘口矩阵分析全场小球
3. 策略引擎评估投注决策
4. 自动执行真实下单（人工模式仅推荐；每轮最多 N 单、不同比赛）
5. 风控监控 (止损/止盈/限额)
6. 记录 AI 决策日志
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, func

from app.database import AsyncSessionLocal
from app.models.user import (
    User, AIConfig, Match, MatchStatus, Bet, BetStatus, BetType,
    Transaction, TransactionType, BookmakerAccount, BookmakerStatus, Odds,
)
from app.ai.analyzer import analyzer
from app.ai.strategy import (
    BetDecision,
    StrategyConfig,
    StrategyEngine,
    decision_passes_strategy,
    effective_strategy_from_ai_config,
    load_fresh_strategy,
)
from app.core.websocket import manager
from app.services.bet_mode import get_user_bet_mode, is_active_mode
from app.services.bookmakers.china_match import is_china_match
from app.services.bookmakers.plugins.ob.odds import is_virtual_match
from app.services.bookmakers.registry import get_connector
from app.core.crypto import decrypt_secret
from app.config import settings

logger = logging.getLogger(__name__)

# 单边模式可下注站点：平博 + OB
SINGLE_SIDE_PROVIDER_NAMES = frozenset({"平博", "OB体育"})
SINGLE_SIDE_PROVIDER_CODES = frozenset({"pinnacle", "ob"})


async def connected_provider_names(db: AsyncSession, user_id: int) -> set[str]:
    """用户已连接站点（enabled + CONNECTED + 真实站）的 provider 名称集合。

    分析/扫描阶段的赔率只从已连接站点加载 —— 未连接站的赔率若参与分析，
    AI 会选中它（decision.provider=ob），执行层只能失败或被动切站。
    """
    from sqlalchemy import select as _sa_select

    from app.models.user import BookmakerAccount, BookmakerStatus
    from app.services.bookmakers.catalog import provider_name
    from app.services.bookmakers.registry import is_real_live_account

    res = await db.execute(
        _sa_select(BookmakerAccount).where(
            BookmakerAccount.user_id == int(user_id),
            BookmakerAccount.enabled.is_(True),
            BookmakerAccount.status == BookmakerStatus.CONNECTED,
            BookmakerAccount.code.in_(list(SINGLE_SIDE_PROVIDER_CODES)),
        )
    )
    names: set[str] = set()
    for acc in res.scalars().all():
        if is_real_live_account(acc.code, acc.base_url or ""):
            names.add(provider_name(acc.code))
    return names


# 每轮最多分析的滚球场次（按站点+球类覆盖）
_LIVE_SCAN_LIMIT = max(
    60, int(getattr(settings, "AI_LIVE_SCAN_LIMIT", 120) or 120)
)
_LIVE_ANALYZE_CONCURRENCY = max(
    2, int(getattr(settings, "AI_ANALYZE_CONCURRENCY", 8) or 8)
)


class AIBettingEngine:
    """
    AI自动投注主引擎

    运行模式:
    - 真实下单：下单一律走体育站真实接口，是否自动执行由用户 bet_mode（人工/自动）控制
    """

    def __init__(self, user_id: int):
        self.user_id = user_id
        self.is_running = False
        self._task: Optional[asyncio.Task] = None
        self._cycle_ai_config = None  # 每轮缓存，避免逐场查 DB
        self._cycle_strat_cfg = None

    # === 生命周期 ===
    async def start(self):
        """启动引擎"""
        if self.is_running:
            logger.warning(f"AI引擎已在运行: user={self.user_id}")
            return

        self.is_running = True
        self._task = asyncio.create_task(self._main_loop())
        logger.info(f"AI投注引擎启动: user={self.user_id}")

    async def stop(self):
        """停止引擎"""
        self.is_running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info(f"AI投注引擎停止: user={self.user_id}")

    async def _main_loop(self):
        """主循环：分析/下单一轮后休眠（默认 120 秒）"""
        try:
            while self.is_running:
                try:
                    await self._run_cycle()
                    # 周期续期跨进程锁/运行标记（TTL 900s，每轮 ~120s 续一次）
                    try:
                        from app.core.cache import cache

                        token = getattr(self, "_engine_lock_token", "")
                        if token:
                            await cache.extend_lock_if_owned(
                                f"ai:engine:lock:{self.user_id}", token, ttl_sec=_ENGINE_LOCK_TTL
                            )
                        await cache.set(f"ai:engine:running:{self.user_id}", "1", ttl=_ENGINE_LOCK_TTL)
                    except Exception:
                        pass
                    # 每轮间隔：默认 120s（热读 settings，改 .env 后重启生效）
                    interval = max(120, int(getattr(settings, "AI_SCAN_INTERVAL_SEC", 120) or 120))
                    logger.info("AI 引擎本轮结束，%s 秒后下一轮", interval)
                    await asyncio.sleep(interval)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"AI引擎异常: {e}", exc_info=True)
                    await asyncio.sleep(settings.AI_RETRY_SLEEP_SEC)
        finally:
            # 引擎退出（停止/风控自停/异常）：清运行标记，让锁随 TTL 自然过期或被下次启动复用
            try:
                from app.core.cache import cache

                await cache.delete(f"ai:engine:running:{self.user_id}")
            except Exception:
                pass

    # === 单次执行周期 ===
    async def _run_cycle(self):
        """执行一次完整的分析+投注周期。

        关键：LLM 分析前必须释放 DB 会话，否则长分析会触发
        idle_in_transaction / connection closed，导致选中后无法下单。
        """
        # --- Phase 1: 短会话准备（读配置/候选）---
        fixture_groups: list[list[int]] = []
        candidates: list[dict] = []
        bet_mode = "manual"
        auto_place = False
        spendable = Decimal("0")
        daily_loss_amt = Decimal("0")
        active_count = 0
        strat_cfg = None
        ai_config = None

        async with AsyncSessionLocal() as db:
            user_result = await db.execute(select(User).where(User.id == self.user_id))
            user = user_result.scalar_one_or_none()
            if not user or not user.ai_enabled:
                self.is_running = False
                return

            ai_config, strat_cfg = await load_fresh_strategy(self.user_id)
            self._cycle_ai_config = ai_config  # 缓存供本轮复用
            self._cycle_strat_cfg = strat_cfg
            if not ai_config or not getattr(ai_config, "is_active", True):
                self.is_running = False
                return
            if not bool(getattr(strat_cfg, "use_llm_analysis", True)):
                logger.info("[AI主循环] user=%s AI分析已关闭，自动下单引擎自停", self.user_id)
                user.ai_enabled = False
                await db.flush()
                await db.commit()
                self.is_running = False
                try:
                    await self._notify(user.id, "ai_disabled", {
                        "message": "AI分析已关闭，自动下单已停止",
                    })
                except Exception:
                    pass
                return

            should_stop, reason = await self._check_risk(db, user, ai_config)
            if should_stop:
                logger.info(f"AI引擎暂停: {reason}")
                await self._notify(user.id, "risk_stop", reason)
                self.is_running = False
                return

            bet_mode = get_user_bet_mode(user)
            auto_place = is_active_mode(user)
            if not auto_place:
                logger.info("[AI主循环] user=%s 人工模式：仅分析推荐，不自动下单", self.user_id)
            else:
                logger.info("[AI主循环] user=%s 自动模式：分析+自动下单", self.user_id)

            candidates = await self._scan_candidates(db, ai_config)
            if not candidates:
                logger.info("[AI主循环] user=%s 本轮无滚球候选赛事", self.user_id)
                return
            logger.info(
                "[AI主循环] user=%s 扫描到 %d 场候选赛事 | bet_mode=%s auto_place=%s",
                self.user_id, len(candidates), bet_mode, auto_place,
            )

            from app.services.fixture_key import group_matches_by_fixture

            class _C:
                __slots__ = ("id", "sport", "home_team", "away_team", "start_time", "external_id", "extra_data")

                def __init__(self, d: dict):
                    self.id = d["id"]
                    self.sport = d.get("sport")
                    self.home_team = d.get("home_team")
                    self.away_team = d.get("away_team")
                    st = d.get("start_time") or ""
                    try:
                        self.start_time = (
                            datetime.fromisoformat(st.replace("Z", "+00:00")) if st else None
                        )
                    except Exception:
                        self.start_time = None
                    self.external_id = f"{d.get('site_code') or ''}:"
                    self.extra_data = {}

            groups = group_matches_by_fixture([_C(c) for c in candidates])
            fixture_groups = [[int(m.id) for m in g] for g in groups]

            logger.info(
                "[AI主循环] user=%s 策略: stake<=%s daily<=%s stop=%s tp=%s | 同场分组=%d 组",
                self.user_id,
                strat_cfg.max_bet_amount,
                strat_cfg.max_daily_bets,
                strat_cfg.stop_loss,
                strat_cfg.take_profit,
                len(fixture_groups),
            )
            daily_loss = await self._calc_daily_pnl(db, user)
            daily_loss_amt = abs(daily_loss) if daily_loss < 0 else Decimal("0")
            active_count = await self._count_active_bets(db, user)
            spendable = await self._spendable_balance(db, user)
            if spendable < settings.AI_MIN_BALANCE:
                logger.info("可用余额不足: %s", spendable)
                return
            await db.commit()
        # Phase 1 结束：连接已释放，再跑 LLM

        await self._notify(self.user_id, "cycle_start", {
            "candidates": len(candidates),
            "fixture_groups": len(fixture_groups),
            "bet_mode": bet_mode,
            "auto_place": auto_place,
        })

        # --- Phase 2 + 3 合并：流式分析→评估→下单（分析完立即下单，不等其他组）---
        user_engine = StrategyEngine(strat_cfg, user_id=self.user_id)
        uid = self.user_id
        sem = asyncio.Semaphore(_LIVE_ANALYZE_CONCURRENCY)
        analyses: list[dict] = []
        decisions: list[BetDecision] = []
        executed_box: list[int] = [0]          # 已下单数（list 便于闭包内修改）
        placed_matches: set[int] = set()
        bet_lock = asyncio.Lock()             # 保护下单环节：风控/防重复/计数
        analysis_summary: list[dict] = []

        async def _analyze_and_place(ids: list[int]) -> Optional[dict]:
            """单个同场分组：分析 → 策略闸门 → 立即下单（流式）。"""
            async with sem:
                # 1. LLM 分析
                try:
                    recs = await analyze_fixture_group(ids, uid, strat_override=strat_cfg)
                    best = pick_best_rec_for_fixture(recs)
                except Exception as e:
                    logger.warning("滚球同场分析失败 ids=%s: %s", ids, e)
                    return None
                if not best or best.get("error"):
                    if best and best.get("error"):
                        logger.warning(
                            "[AI主循环] 同场分析返回错误: %s vs %s | error=%s",
                            best.get("home_team", "?"), best.get("away_team", "?"),
                            best.get("error"),
                        )
                    return best
                analyses.append(best)
                rec = best.get("recommendation") or {}
                sel = str(rec.get("selection") or "").lower()
                if sel != "under":
                    skip_reason = str(
                        rec.get("reasoning")
                        or (best.get("analysis") or {}).get("reasoning")
                        or "AI 未给出可下单的小球方向"
                    )
                    if sel == "skip":
                        logger.info(
                            "[AI主循环] AI 判定跳过（数据不足）: match=%s %s vs %s",
                            best.get("match_id"), best.get("home_team", "?"), best.get("away_team", "?"),
                        )
                    else:
                        logger.info(
                            "[AI主循环] 跳过无效方向: match=%s %s vs %s | sel=%s",
                            best.get("match_id"), best.get("home_team", "?"), best.get("away_team", "?"), sel,
                        )
                    await self._notify(self.user_id, "analysis_done", {
                        "match_id": best.get("match_id"),
                        "home_team": best.get("home_team", "?"),
                        "away_team": best.get("away_team", "?"),
                        "selection": "skip",
                        "confidence": 0.0,
                        "odds": 0.0,
                        "should_bet": False,
                        "bet_type": rec.get("bet_type") or "total",
                        "status": "skipped",
                        "reasoning": skip_reason,
                    })
                    return best

                # 2. 策略闸门评估（无 DB）
                bt = "total"
                odds_map = dict(best.get("current_odds") or {})
                if rec.get("odds"):
                    odds_map[sel] = float(rec["odds"])
                conf = float(rec.get("confidence") or (best.get("analysis") or {}).get("confidence") or 0)
                async with bet_lock:
                    active_count_now = active_count + executed_box[0]
                decision = await user_engine.evaluate_bet(
                    match_info={
                        "id": int(best["match_id"]),
                        "odds": odds_map,
                        "provider_code": str(rec.get("provider_code") or ""),
                        "home_team": best.get("home_team"),
                        "away_team": best.get("away_team"),
                        "league": best.get("league"),
                        "sport": best.get("sport"),
                        "period": best.get("period"),
                        "clock": best.get("clock"),
                        "home_score": best.get("home_score"),
                        "away_score": best.get("away_score"),
                    },
                    analysis={
                        **(best.get("analysis") or {}),
                        "prediction": sel,
                        "bet_type": bt,
                        "confidence": conf,
                        "odds": float(rec.get("odds") or 0),
                        "provider_code": str(rec.get("provider_code") or ""),
                        "line": rec.get("line"),
                        "consensus_reached": bool((best.get("analysis") or {}).get("consensus_reached")),
                        "reasoning": str(rec.get("reasoning") or ""),
                    },
                    user_balance=spendable,
                    daily_loss=daily_loss_amt,
                    active_bets_count=active_count_now,
                )
                stake = Decimal(str(decision.suggested_stake or 0))
                if stake < Decimal("1.00"):
                    stake = Decimal("1.00")
                decision = decision.model_copy(update={"suggested_stake": stake})
                decisions.append(decision)
                # 回写 recommendation 展示字段
                if best.get("recommendation") is not None:
                    best["recommendation"] = {
                        **(best.get("recommendation") or {}),
                        "should_bet": bool(decision.should_bet),
                        "confidence": float(decision.confidence or conf),
                        "win_rate": round(float(decision.confidence or conf) * 100, 1),
                        "suggested_stake": float(decision.suggested_stake or 0),
                        "reasoning": decision.reasoning,
                        "selection": decision.selection,
                        "odds": float(decision.odds or rec.get("odds") or 0),
                        "provider_code": decision.provider_code or rec.get("provider_code"),
                        "line": decision.line if decision.line is not None else rec.get("line"),
                    }
                rec_event = best.get("recommendation") or rec

                await self._notify(self.user_id, "analysis_done", {
                    "match_id": best.get("match_id"),
                    "home_team": best.get("home_team", "?"),
                    "away_team": best.get("away_team", "?"),
                    "selection": rec_event.get("selection", ""),
                    "confidence": float(
                        rec_event.get("raw_confidence")
                        or rec_event.get("confidence")
                        or (best.get("analysis") or {}).get("confidence")
                        or 0
                    ),
                    "odds": float(rec_event.get("raw_odds") or rec_event.get("odds") or 0),
                    "should_bet": bool(decision.should_bet),
                    "bet_type": rec_event.get("bet_type") or "total",
                })

                if not decision.should_bet:
                    return best

                logger.info(
                    "✅ 策略通过（流式） | match=%s %s vs %s | %s/%s | conf=%.2f odds=%.2f stake=$%s",
                    best.get("match_id"), best.get("home_team", "?"), best.get("away_team", "?"),
                    decision.bet_type, decision.selection,
                    float(decision.confidence or 0), float(decision.odds or 0),
                    decision.suggested_stake,
                )

                # 3. 立即下单（加锁保护：防重复/风控）
                async with bet_lock:
                    mid = int(decision.match_id or 0)
                    if mid in placed_matches:
                        logger.info("[AI主循环] 跳过同场重复: match=%s", mid)
                        return best
                    # 独立 session 下单（async session 不能跨任务共享）
                    async with AsyncSessionLocal() as db:
                        user = await db.get(User, self.user_id)
                        if not user or not user.ai_enabled:
                            self.is_running = False
                            return best
                        ai_config2 = self._cycle_ai_config
                        strat_cfg2 = self._cycle_strat_cfg
                        if not ai_config2:
                            return best
                        # 风控实时检查
                        should_stop, reason = await self._check_risk(db, user, ai_config2)
                        if should_stop:
                            logger.info("[AI主循环] ❌ 下单前触发风控，停止: %s", reason)
                            return best
                        # 跨轮次防重复
                        if mid > 0 and await self._match_already_bet(db, self.user_id, mid):
                            logger.info("[AI主循环] 跳过今日已下注比赛: match=%s", mid)
                            return best
                        # 最新策略二次校验
                        ok_pass, why = decision_passes_strategy(decision, strat_cfg2)
                        if not ok_pass:
                            logger.info("[AI主循环] ❌ 最新策略拦截 match=%s: %s", decision.match_id, why)
                            return best
                        if not auto_place:
                            await self._notify(user.id, "manual_recommend", {
                                **decision.model_dump(),
                                "bet_mode": bet_mode,
                                "strategy": strat_cfg2.model_dump(),
                                "message": "人工模式：已按最新 AI 策略生成推荐，请手动确认后真实下单",
                            })
                            return best
                        ok = await self._execute_bet(
                            db,
                            user,
                            decision,
                            analysis_cache=best.get("analysis") or {},
                            ai_config=ai_config2,
                        )
                        if ok:
                            executed_box[0] += 1
                            if mid > 0:
                                placed_matches.add(mid)
                        await db.commit()
                return best

        # 并发跑所有同场分组：分析完立即评估+下单
        await asyncio.gather(*[_analyze_and_place(g) for g in fixture_groups])

        approved = [d for d in decisions if d.should_bet]
        executed = executed_box[0]
        logger.info(
            "[AI主循环] 流式分析+下单完成 | 分组=%d 分析=%d 通过=%d 执行=%d | 模式=%s",
            len(fixture_groups), len(analyses), len(approved), executed, bet_mode,
        )

        # 发布推荐缓存（前端展示）
        await self._publish_analyses_to_recs_cache(analyses)

        # 构建分析摘要供前端日志展示
        for item in analyses:
            rec = item.get("recommendation") or {}
            ana = item.get("analysis") or {}
            selection = str(rec.get("selection") or "").lower()
            skipped = selection != "under"
            conf_pct = float(
                rec.get("raw_win_rate")
                or rec.get("win_rate")
                or 0
            )
            if skipped:
                conf_pct = 0.0
            elif conf_pct <= 0:
                conf = float(rec.get("raw_confidence") or rec.get("confidence") or ana.get("confidence") or 0)
                if conf > 1:
                    conf /= 100.0
                conf_pct = conf * 100.0
            analysis_summary.append({
                "home_team": item.get("home_team", "?"),
                "away_team": item.get("away_team", "?"),
                "bet_type": rec.get("bet_type") or ana.get("bet_type") or "total",
                "selection": "skip" if skipped else selection,
                "confidence": round(conf_pct, 1),
                "odds": 0.0 if skipped else float(rec.get("raw_odds") or rec.get("odds") or 0),
                "should_bet": bool(rec.get("should_bet")),
                "status": "skipped" if skipped else "rejected",
                "reasoning": str(
                    rec.get("reasoning")
                    or ana.get("reasoning")
                    or "AI 未给出可下单的小球方向"
                ),
            })

        if not approved:
            logger.info(
                "本轮已分析 %s 场，无符合 AI 策略的自动投注机会（模式=%s）",
                len(analyses),
                bet_mode,
            )
            await self._notify(self.user_id, "cycle_done", {
                "analyzed": len(analyses),
                "approved": 0,
                "executed": 0,
                "bet_mode": bet_mode,
                "auto_place": auto_place,
                "analysis_summary": analysis_summary,
                "message": (
                    "自动模式：本轮无策略通过场次"
                    if auto_place
                    else "人工模式：已更新高胜率推荐，请手动确认"
                ),
            })
            return

        logger.info(
            "[AI主循环] 本轮完成 | 候选=%d 分析=%d 通过=%d 执行=%d | 模式=%s",
            len(candidates), len(analyses), len(approved), executed, bet_mode,
        )

        await self._notify(self.user_id, "cycle_complete", {
            "analyzed": len(analyses),
            "candidates": len(candidates),
            "approved": len(approved),
            "executed": executed,
            "placed_matches": sorted(placed_matches),
            "bet_mode": bet_mode,
            "auto_place": auto_place,
            "market": "total",
            "scope": "live",
            "analysis_summary": analysis_summary,
            "decisions": [d.model_dump() for d in decisions if d.should_bet],
        })

    async def _publish_analyses_to_recs_cache(self, analyses: list[dict]) -> None:
        """把本轮全部分析结果写入推荐缓存；前端再按人工/自动模式过滤。"""
        if not analyses:
            return
        try:
            from app.ai.recs_job import _write_provider_caches

            by_sport: dict[str, list] = {}
            for item in analyses:
                if not item or item.get("error"):
                    continue
                sport_k = str(item.get("sport") or "football").lower()
                if sport_k not in ("football", "basketball"):
                    sport_k = "football"
                by_sport.setdefault(sport_k, []).append(item)

            for sport_k, items in by_sport.items():
                await _write_provider_caches(
                    self.user_id,
                    sport_k,
                    items,
                    limit=max(len(items), int(getattr(settings, "AI_RECS_LIMIT", 80) or 80)),
                    progress=len(items),
                    total=len(items),
                )
        except Exception as e:
            logger.warning("publish recs cache failed: %s", e)

    # === 赛事扫描 ===
    async def _scan_candidates(self, db: AsyncSession, ai_config: AIConfig | None) -> list[dict]:
        """扫描 OB / 平博 滚球足球/篮球，且具备盘口的候选。"""
        from app.services.provider_utils import site_code_from_match

        preferred_raw = (getattr(ai_config, "preferred_sports", None) or []) if ai_config else []
        preferred = [str(x).lower() for x in preferred_raw if x]
        sports = preferred or ["football", "basketball"]
        sports = [s for s in sports if s in ("football", "basketball", "soccer")]
        if not sports:
            sports = ["football", "basketball"]
        # soccer → football
        sports = ["football" if s == "soccer" else s for s in sports]

        from app.services.fixture_key import group_matches_by_fixture, same_fixture

        today = datetime.now(timezone.utc).date()
        today_start = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)
        existing_bets = await db.execute(
            select(Bet.match_id).where(
                Bet.user_id == self.user_id,
                Bet.status == BetStatus.SUCCESS,
                Bet.created_at >= today_start,
            )
        )
        bet_match_ids = {int(r[0]) for r in existing_bets.all() if r[0] is not None}

        bet_matches: list[Match] = []
        if bet_match_ids:
            bet_rows = await db.execute(select(Match).where(Match.id.in_(list(bet_match_ids))))
            bet_matches = list(bet_rows.scalars().all())

        query = select(Match).where(
            and_(
                Match.status == MatchStatus.LIVE,
                Match.sport.in_(sports),
            )
        )
        result = await db.execute(query.order_by(Match.start_time.desc()).limit(_LIVE_SCAN_LIMIT * 3))
        matches = list(result.scalars().all())

        # 已在任一同场站点下过单 → 整场跳过
        def _already_bet(m: Match) -> bool:
            if int(m.id) in bet_match_ids:
                return True
            return any(same_fixture(m, bm) for bm in bet_matches)

        from app.ai.analysis_filters import skip_reason_for_match, sort_just_started_first

        from app.ai.strategy_gates import team_is_excluded

        # 只分析已连接站点可下注的赔率：未连接站（如未开 OB）的赔率参与分析
        # 会让 AI 选中它，执行时只能失败/被动切站（下单失败根因）
        connected_names = await connected_provider_names(db, self.user_id)
        if not connected_names:
            logger.info("[AI扫描] 无已连接站点，跳过本轮扫描 user=%s", self.user_id)
            return []

        excluded = list(getattr(ai_config, "excluded_teams", None) or [])

        # 刚开赛优先扫描
        matches = sort_just_started_first(matches)
        candidates = []
        pre_filtered: list = []
        for m in matches:
            if _already_bet(m):
                continue
            sport_key = m.sport.value if hasattr(m.sport, "value") else str(m.sport)
            if is_virtual_match(sport_key, m.league or "", m.home_team or "", m.away_team or ""):
                continue
            if is_china_match(m.league or "", m.home_team or "", m.away_team or "", sport_key):
                continue
            if team_is_excluded(m.home_team or "", m.away_team or "", excluded):
                continue
            site = site_code_from_match(m)
            if site not in SINGLE_SIDE_PROVIDER_CODES:
                continue
            # Pre-check skip reasons that don't need odds (ending_soon, china_match)
            # to avoid loading odds for matches that will be skipped anyway
            why = skip_reason_for_match(m, None)
            if why:
                logger.debug("scan skip match=%s reason=%s", m.id, why)
                continue
            pre_filtered.append((m, sport_key, site))

        # 并行加载赔率包（串行 N 场 -> 并行 gather，大幅缩短扫描耗时）
        from app.ai.market_recommend import load_all_market_odds_pack

        async def _load_odds(m, sport_key, site):
            try:
                pack = await load_all_market_odds_pack(
                    db, m.id, sport_key, providers_filter=connected_names
                )
                return m, sport_key, site, pack
            except Exception:
                return m, sport_key, site, None

        odds_results = await asyncio.gather(
            *[_load_odds(m, sk, st) for m, sk, st in pre_filtered]
        ) if pre_filtered else []

        for m, sport_key, site, odds_pack in odds_results:
            if not odds_pack:
                continue
            markets_odds = odds_pack.get("markets") or {}
            odds = dict(odds_pack.get("flat") or {})
            if not markets_odds and not odds:
                continue
            total_line = None
            if isinstance(markets_odds.get("total"), dict):
                total_line = markets_odds["total"].get("line")
            why = skip_reason_for_match(
                m,
                total_line,
                odds_map={"markets": markets_odds, **odds},
            )
            if why:
                # 赔率区间外也跳过：E1 闸门会再次拦截，放行只浪费 LLM 调用
                logger.debug("scan skip match=%s reason=%s", m.id, why)
                continue

            candidates.append({
                "id": m.id,
                "sport": sport_key,
                "league": m.league,
                "home_team": m.home_team,
                "away_team": m.away_team,
                "start_time": m.start_time.isoformat() if m.start_time else "",
                "venue": m.venue,
                "odds": {"markets": markets_odds, "line_movements": odds_pack.get("line_movements") or {}, **odds},
                "site_code": site,
                "total_line": total_line,
            })

        # 扫描上限按「同场」计；保持刚开赛优先顺序取组
        if candidates:
            class _T:
                __slots__ = ("id", "sport", "home_team", "away_team", "start_time")

                def __init__(self, d):
                    self.id = d["id"]
                    self.sport = d["sport"]
                    self.home_team = d["home_team"]
                    self.away_team = d["away_team"]
                    st = d.get("start_time") or ""
                    try:
                        self.start_time = (
                            datetime.fromisoformat(st.replace("Z", "+00:00")) if st else None
                        )
                    except Exception:
                        self.start_time = None

            objs = [_T(c) for c in candidates]
            id_to_group: dict[int, list] = {}
            for g in group_matches_by_fixture(objs):
                for m in g:
                    id_to_group[int(m.id)] = g
            seen_g: set[int] = set()
            ordered_groups: list = []
            for c in candidates:
                g = id_to_group.get(int(c["id"]))
                if not g:
                    continue
                gid = id(g)
                if gid in seen_g:
                    continue
                seen_g.add(gid)
                ordered_groups.append(g)
            kept_ids: set[int] = set()
            for g in ordered_groups[:_LIVE_SCAN_LIMIT]:
                for m in g:
                    kept_ids.add(int(m.id))
            candidates = [c for c in candidates if int(c["id"]) in kept_ids]

        return candidates
    # === 下单执行 ===
    async def _execute_bet(
        self,
        db: AsyncSession,
        user: User,
        decision: BetDecision,
        analysis_cache: dict,
        ai_config: AIConfig | None = None,
    ) -> bool:
        """真实下单到体育站（仅自动模式调用；禁止本地假单）。严格按 AI 策略二次校验。"""
        if not is_active_mode(user):
            await self._notify(user.id, "manual_recommend", {
                **decision.model_dump(),
                "message": "人工模式：跳过自动下单",
            })
            return False

        if ai_config is None:
            logger.warning("无 AI 策略配置，拒绝自动下单 user=%s", user.id)
            return False
        strat_cfg = effective_strategy_from_ai_config(ai_config)

        odds_result = await db.execute(
            select(Match).where(Match.id == decision.match_id)
        )
        match = odds_result.scalar_one_or_none()
        if not match:
            return False

        # 同场已有未结算单 → 跳过（OB/平博只投一单）
        from app.services.fixture_key import sibling_match_ids

        try:
            sib_ids = await sibling_match_ids(db, match)
        except Exception:
            sib_ids = [int(match.id)]
        open_bet = await db.execute(
            select(Bet.id).where(
                Bet.user_id == user.id,
                Bet.match_id.in_(sib_ids),
                Bet.status == BetStatus.SUCCESS,
            ).limit(1)
        )
        if open_bet.scalar_one_or_none() is not None:
            logger.info("同场已有注单，跳过 AI 下单 match=%s sibs=%s", match.id, sib_ids)
            return False

        sport_key = match.sport.value if hasattr(match.sport, "value") else str(match.sport)
        if is_virtual_match(sport_key, match.league or "", match.home_team or "", match.away_team or ""):
            logger.info(f"跳过虚拟赛事 AI 下单: match={match.id} league={match.league}")
            return False
        if is_china_match(match.league or "", match.home_team or "", match.away_team or "", sport_key):
            logger.info("跳过中国赛事 AI 下单: match=%s league=%s", match.id, match.league)
            return False

        sel = str(decision.selection or "").lower()
        bet_type = str(getattr(decision, "bet_type", None) or "total").lower()
        if bet_type != "total" or sel != "under":
            logger.warning(
                "[AI下单] ❌ 不支持的盘口/方向: match=%s type=%s sel=%s",
                decision.match_id, bet_type, sel,
            )
            return False

        logger.info(
            "[AI下单] 准备下单 match=%s %s vs %s | sel=%s conf=%.2f odds=%.2f stake=%.2f | provider=%s",
            decision.match_id, match.home_team or "?", match.away_team or "?",
            sel, float(decision.confidence or 0), float(decision.odds or 0),
            float(decision.suggested_stake or 0), str(decision.provider_code or "?"),
        )

        from app.services.bookmakers.catalog import provider_name
        from app.services.bookmakers.registry import is_real_live_account
        from app.services.provider_utils import site_code_from_match

        pack = await self._get_best_market_pack(
            db,
            decision.match_id,
            bet_type,
            providers_filter=SINGLE_SIDE_PROVIDER_NAMES,
        )
        best_meta = (pack.get("best_by_selection") or {}).get(sel) or {}
        provider_code = str(
            decision.provider_code or best_meta.get("provider_code") or site_code_from_match(match) or ""
        ).lower()
        if provider_code not in SINGLE_SIDE_PROVIDER_CODES:
            ext0 = str(match.external_id or "")
            if ext0.startswith("ob:"):
                provider_code = "ob"
            elif ext0.startswith("pinnacle:"):
                provider_code = "pinnacle"
            else:
                provider_code = "pinnacle"
        provider_label = provider_name(provider_code)

        # --- 检查站点连接状态，未连接则自动切换到已连接的站点 ---
        from app.services.provider_utils import code_by_provider as _code_by_provider
        conn_res = await db.execute(
            select(BookmakerAccount).where(
                BookmakerAccount.user_id == user.id,
                BookmakerAccount.enabled.is_(True),
                BookmakerAccount.status == BookmakerStatus.CONNECTED,
                BookmakerAccount.code.in_(list(SINGLE_SIDE_PROVIDER_CODES)),
            )
        )
        connected_codes: set[str] = set()
        for acc in conn_res.scalars().all():
            if is_real_live_account(acc.code, acc.base_url or ""):
                connected_codes.add(acc.code)

        if provider_code not in connected_codes:
            # 尝试切换到已连接且有赔率的站点
            odds_by_provider = pack.get("odds_by_provider") or {}
            switched = False
            for pname, sel_odds in odds_by_provider.items():
                alt_code = _code_by_provider(pname) or ""
                if alt_code not in connected_codes or alt_code == provider_code:
                    continue
                if sel not in (sel_odds or {}):
                    continue
                logger.info(
                    "[AI下单] 站点切换: %s未连接 -> 切换至%s | match=%s sel=%s odds=%.2f",
                    provider_label, provider_name(alt_code), decision.match_id, sel, float(sel_odds[sel]),
                )
                provider_code = alt_code
                provider_label = provider_name(provider_code)
                best_meta = {
                    "provider": pname,
                    "provider_code": alt_code,
                    "odds": float(sel_odds[sel]),
                }
                switched = True
                break
            if not switched:
                logger.warning(
                    "AI 下单失败：站点未连接且无可用替代 site=%s connected=%s",
                    provider_code, connected_codes or "无",
                )
                await self._notify(user.id, "bet_failed", {
                    "match_id": decision.match_id,
                    "message": f"请先连接{provider_label}后再自动下单",
                })
                return False

        ids = dict((match.extra_data or {}).get("ids") or {})
        match_ext = str(ids.get(provider_code) or "")
        if not match_ext and str(match.external_id or "").startswith(f"{provider_code}:"):
            match_ext = str(match.external_id)
        if not match_ext:
            logger.warning("AI 下单缺少赛事 ID: match=%s site=%s", decision.match_id, provider_code)
            await self._notify(user.id, "bet_failed", {
                "match_id": decision.match_id,
                "message": "缺少对应站点赛事 ID，请先同步该站滚球",
            })
            return False

        bt_enum = BetType.TOTAL
        odds_row = await self._get_odds_row(
            db,
            decision.match_id,
            provider_name_prefer=provider_label,
            bet_type=bt_enum,
        )
        odds_payload = dict(odds_row.odds_data or {}) if odds_row else {}
        line_val = None
        # line 必须来自下单站点自己的行：兜底行可能是兄弟站的（两家 line 常差
        # 0.25/0.5），拿别站的线结算会错判输赢。行 provider 不匹配时宁可走
        # decision.line（分析时刻的 canonical 线）也不用错行。
        row_is_own_site = bool(odds_row and str(odds_row.provider or "") == provider_label)
        if bet_type == "total":
            if row_is_own_site and odds_row.total is not None:
                try:
                    line_val = float(odds_row.total)
                except (TypeError, ValueError):
                    line_val = None
        if line_val is None and decision.line is not None:
            try:
                line_val = float(decision.line)
            except (TypeError, ValueError):
                line_val = None
        if line_val is None and pack.get("line") is not None:
            try:
                line_val = float(pack["line"])
            except (TypeError, ValueError):
                line_val = None
        if line_val is not None:
            if bet_type == "total":
                odds_payload = {**odds_payload, "line": line_val, "total": line_val}

        try:
            # 优先用数据库最新赔率，其次用决策赔率
            fresh_sel_odds = float(odds_payload.get(sel) or 0)
            current_odds = float(
                fresh_sel_odds
                or decision.odds
                or (best_meta.get("odds") or 0)
                or (pack.get("odds") or {}).get(sel)
                or 0
            )
        except (TypeError, ValueError):
            current_odds = 0.0
        if current_odds <= 0:
            logger.warning(
                "⚠️ 下单拒绝 | match=%s | 门禁=赔率无效 | type=%s sel=%s odds=0",
                decision.match_id, bet_type, sel,
            )
            return False

        # 赔率变动只记录不拦截，避免分析通过后被市场波动再次挡回去
        decision_odds = float(decision.odds or 0)
        if decision_odds > 0 and abs(current_odds - decision_odds) > 0.05:
            if current_odds > decision_odds + 0.05:
                logger.info(
                    "📋 赔率上调 | match=%s | 决策=%.2f 最新=%.2f | 继续使用最新赔率",
                    decision.match_id, decision_odds, current_odds,
                )
            else:
                logger.info(
                    "📋 赔率变动 | match=%s | 决策=%.2f 最新=%.2f | 继续使用最新赔率",
                    decision.match_id, decision_odds, current_odds,
                )

        # AI 分析已决定，直接下单

        site_res = await db.execute(
            select(BookmakerAccount).where(
                BookmakerAccount.user_id == user.id,
                BookmakerAccount.enabled.is_(True),
                BookmakerAccount.status == BookmakerStatus.CONNECTED,
                BookmakerAccount.code == provider_code,
            )
        )
        site_acc = None
        for acc in site_res.scalars().all():
            if is_real_live_account(acc.code, acc.base_url or ""):
                site_acc = acc
                break
        if not site_acc:
            logger.warning("AI 下单失败：站点未连接 site=%s", provider_code)
            await self._notify(user.id, "bet_failed", {
                "match_id": decision.match_id,
                "message": f"请先连接{provider_label}后再自动下单",
            })
            return False

        # 仓位为 0/负 = 异常决策，直接拒绝下单（绝不静默回退到满仓上限）
        stake = Decimal(str(decision.suggested_stake or 0)).quantize(Decimal("0.01"))
        if stake < Decimal("1.00"):
            logger.warning(
                "[AI下单] ❌ 拒绝 match=%s | 仓位异常 suggested_stake=%s（不回退满仓）",
                decision.match_id, decision.suggested_stake,
            )
            return False
        if float(site_acc.balance or 0) < float(stake):
            logger.warning("站点余额不足: need=%s, have=%s", stake, site_acc.balance)
            return False

        connector = get_connector(
            provider_code,
            base_url=site_acc.base_url,
            username=site_acc.username,
            password=decrypt_secret(site_acc.password_encrypted),
            balance=site_acc.balance,
            session_token=decrypt_secret(site_acc.session_token_encrypted),
            profile=site_acc.profile_json if isinstance(site_acc.profile_json, dict) else {},
        )
        # === 下单 + 补单重试 ===
        # 暂停滚球轮询器，避免 lane 锁竞争
        _resume_fn = None
        try:
            from app.services.bookmakers.live_poller import pause_live_poller, resume_live_poller
            pause_live_poller()
            _resume_fn = resume_live_poller
        except Exception:
            pass

        retry_count = int(settings.BET_RETRY_COUNT)
        retry_delay = float(settings.BET_RETRY_DELAY)
        place = None
        for attempt in range(1 + retry_count):
            if attempt > 0:
                logger.info(
                    "补单重试 %s/%s: match=%s 等待 %.1fs",
                    attempt, retry_count, decision.match_id, retry_delay,
                )
                await asyncio.sleep(retry_delay)
                # 重新获取最新赔率
                fresh_odds_row = await self._get_odds_row(
                    db,
                    decision.match_id,
                    provider_name_prefer=provider_label,
                    bet_type=bt_enum,
                )
                if fresh_odds_row:
                    fresh_payload = dict(fresh_odds_row.odds_data or {})
                    if line_val is not None:
                        if bet_type == "total":
                            fresh_payload = {**fresh_payload, "line": line_val, "total": line_val}
                    odds_payload = fresh_payload
                    try:
                        current_odds = float(
                            fresh_payload.get(sel)
                            or fresh_payload.get("odds")
                            or current_odds
                        )
                    except (TypeError, ValueError):
                        pass
                logger.info(
                    "补单重试 %s/%s: match=%s 最新赔率=%.3f",
                    attempt, retry_count, decision.match_id, current_odds,
                )

            # 队名注入 odds_data：UI 下单依赖队名定位赛事行
            # （短格式 external_id 如 pinnacle:1634071712 无法解析队名）
            odds_payload["_home_team"] = match.home_team or ""
            odds_payload["_away_team"] = match.away_team or ""

            place = await connector.place_bet(
                match_external_id=str(match_ext or match.external_id),
                selection=sel,
                odds=float(current_odds),
                stake=stake,
                bet_type=bet_type,
                odds_data=odds_payload,
            )
            if place.ok:
                if provider_code == "ob" and place.external_bet_id:
                    await self._mark_bet_pending(
                        decision.match_id,
                        place.external_bet_id,
                        selection=sel,
                        bet_type=bet_type,
                        odds=current_odds,
                        stake=stake,
                        line=line_val,
                        confidence=decision.confidence,
                        reasoning=decision.reasoning,
                        provider=provider_label,
                    )
                    logger.info(
                        "OB 下单回执已返回 orderNo=%s，按成功受理，不再做存在性验证",
                        place.external_bet_id,
                    )
            if place.ok:
                break

            logger.warning(
                "AI 下单失败 (attempt %s/%s): %s",
                attempt + 1, 1 + retry_count, place.message,
            )

        if not place or not place.ok:
            msg = place.message if place else "unknown"
            logger.warning("AI 真实下单失败（已重试 %s 次）: %s", retry_count, msg)
            await self._notify(user.id, "bet_failed", {
                "match_id": decision.match_id,
                "message": msg or f"{provider_label}下单失败",
            })
            if _resume_fn:
                _resume_fn()
            return False

        try:
            bal = await connector.fetch_balance()
            site_acc.balance = bal
        except Exception:
            if place.balance_after and place.balance_after > 0:
                site_acc.balance = place.balance_after
            else:
                site_acc.balance = Decimal(str(site_acc.balance or 0)) - stake

        potential_payout = (stake * Decimal(str(current_odds))).quantize(Decimal("0.01"))
        line_tag = f" {line_val}" if line_val is not None else ""
        bet = Bet(
            user_id=user.id,
            match_id=decision.match_id,
            bet_type=bet_type,
            selection=sel,
            odds=current_odds,
            stake=stake,
            potential_payout=potential_payout,
            actual_payout=Decimal("0"),  # 未结算，由 bet_settlement 按完场比分写回
            line=float(line_val) if line_val is not None else None,
            status=BetStatus.SUCCESS,
            is_ai_bet=True,
            ai_confidence=decision.confidence,
            ai_reasoning=decision.reasoning,
            provider=provider_label,
            external_bet_id=place.external_bet_id,
        )
        db.add(bet)
        await db.flush()

        type_label = "大小"
        tx = Transaction(
            user_id=user.id,
            type=TransactionType.AI_BET,
            amount=Decimal("0"),
            balance_after=user.balance,
            bet_id=bet.id,
            description=(
                f"AI滚球{type_label}: {match.home_team} vs {match.away_team} "
                f"[{sel}{line_tag} @ {current_odds}]"
            ),
        )
        db.add(tx)

        logger.info(
            "[AI下单] ✅ 成功 match=%s | sel=%s line=%s stake=%.2f odds=%.2f conf=%.2f ext=%s | 预计赔付=%.2f",
            decision.match_id, sel, line_val, float(stake), current_odds,
            float(decision.confidence or 0), place.external_bet_id,
            float(potential_payout),
        )

        await self._notify(user.id, "bet_placed", {
            "bet_id": bet.id,
            "match_id": decision.match_id,
            "selection": sel,
            "bet_type": bet_type,
            "line": line_val,
            "stake": float(stake),
            "odds": current_odds,
            "confidence": decision.confidence,
            "reasoning": decision.reasoning,
            "external_bet_id": place.external_bet_id,
            "provider": provider_label,
        })
        # 已入库，清除 Redis 待定标记
        try:
            from app.core.cache import cache
            await cache.delete(f"ai:bet:pending:{self.user_id}:{decision.match_id}")
        except Exception:
            pass
        if _resume_fn:
            _resume_fn()
        return True

    # === 风控检查 ===
    async def _get_site_balances(self, db: AsyncSession, user: User) -> list[tuple[str, Decimal]]:
        """OB/平博真实账户余额列表 [(code, balance), ...]。"""
        try:
            from app.services.bookmakers.registry import is_real_live_account

            res = await db.execute(
                select(BookmakerAccount).where(
                    BookmakerAccount.user_id == user.id,
                    BookmakerAccount.code.in_(["ob", "pinnacle"]),
                    BookmakerAccount.status == BookmakerStatus.CONNECTED,
                )
            )
            return [
                (acc.code, Decimal(str(acc.balance or 0)))
                for acc in res.scalars().all()
                if is_real_live_account(acc.code, acc.base_url or "")
            ]
        except Exception:
            return []

    async def _check_risk(self, db: AsyncSession, user: User, config: AIConfig) -> tuple[bool, str]:
        """综合风控检查（与人工一键共用策略门禁）。"""
        from app.ai.strategy import effective_strategy_from_ai_config
        from app.ai.strategy_gates import check_daily_risk, stake_bounds

        strat = effective_strategy_from_ai_config(config)
        triggered, why = await check_daily_risk(db, user.id, strat)
        if triggered:
            return True, why

        # 余额检查：需至少能覆盖策略单笔上限（或余额≥1）
        balances = await self._get_site_balances(db, user)
        site_bal = max((bal for _, bal in balances), default=Decimal("0"))
        spendable = site_bal if site_bal > 0 else Decimal(str(user.balance or 0))
        _, hi = stake_bounds(strat)
        need = min(hi, Decimal("1"))
        if spendable < need:
            return True, f"余额不足: {spendable} < {need}"

        return False, ""

    async def _spendable_balance(self, db: AsyncSession, user: User) -> Decimal:
        """OB/平博真实账户可用余额（取较大者）。"""
        balances = await self._get_site_balances(db, user)
        site_bal = max((bal for _, bal in balances), default=Decimal("0"))
        return site_bal if site_bal > 0 else Decimal(str(user.balance or 0))

    # === 辅助方法 ===
    async def _get_odds_row(
        self,
        db: AsyncSession,
        match_id: int,
        *,
        provider_name_prefer: str = "平博",
        bet_type: BetType = BetType.TOTAL,
    ) -> Optional[Odds]:
        """优先取指定站盘口行（默认全场小球）。

        canonical=OB 时平博赔率存在兄弟 match_id 名下（跨站同场未合并），
        扩展查询兄弟场后再按 provider 优先挑选。
        """
        match_ids = [int(match_id)]
        try:
            from app.models.user import Match
            from app.services.fixture_key import sibling_match_ids

            m = await db.get(Match, int(match_id))
            if m is not None:
                for sid in await sibling_match_ids(db, m):
                    if sid and int(sid) not in match_ids:
                        match_ids.append(int(sid))
        except Exception:
            pass

        result = await db.execute(
            select(Odds).where(
                Odds.match_id.in_(match_ids),
                Odds.bet_type == bet_type,
                Odds.provider == provider_name_prefer,
                Odds.valid_to.is_(None),
            ).limit(1)
        )
        row = result.scalar_one_or_none()
        if row:
            return row
        result = await db.execute(
            select(Odds).where(
                Odds.match_id.in_(match_ids),
                Odds.bet_type == bet_type,
                Odds.valid_to.is_(None),
            )
        )
        odds_list = result.scalars().all()
        if odds_list:
            # 无 prefer 站行时兜底：平博行优先（其 _site 带 API 下单所需 sel_id），
            # 但绝不因此改变调用方已确定的下单站点 —— line 由调用方按行 provider 校验
            for o in odds_list:
                if str(o.provider or "") == "平博":
                    return o
            return odds_list[0]
        # 兜底：任意有效行
        result = await db.execute(
            select(Odds).where(
                Odds.match_id.in_(match_ids),
                Odds.valid_to.is_(None),
            )
        )
        return result.scalars().first()

    async def _get_best_market_pack(
        self,
        db: AsyncSession,
        match_id: int,
        bet_type: str = "total",
        *,
        providers_filter: set[str] | None = None,
    ) -> dict:
        """精选指定盘口（total/moneyline/spread）。"""
        from app.ai.market_recommend import load_market_matrix
        from app.services.provider_utils import (
            best_by_selection as _best_by_selection,
            code_by_provider as _code_by_provider,
        )

        bt = str(bet_type or "total").lower()
        matrix, spread_line, total_line = await load_market_matrix(
            db, match_id, bt, providers_filter=providers_filter
        )
        best_raw = _best_by_selection(matrix)
        odds = {sel: float(pair[1]) for sel, pair in best_raw.items()}
        best_by_selection = {}
        for sel, (pname, oval) in best_raw.items():
            code = _code_by_provider(pname) or ""
            best_by_selection[sel] = {
                "provider": pname,
                "provider_code": code,
                "odds": float(oval),
            }
        odds_by_provider: dict[str, dict[str, float]] = {}
        for sel, providers in (matrix or {}).items():
            for pname, oval in (providers or {}).items():
                odds_by_provider.setdefault(pname, {})[sel] = float(oval)
        line = None
        if bt == "total" and total_line:
            line = float(total_line)
        elif bt == "spread" and spread_line:
            line = float(spread_line)
        return {
            "odds": odds,
            "best_by_selection": best_by_selection,
            "odds_by_provider": odds_by_provider,
            "line": line,
            "bet_type": bt,
        }

    async def _calc_daily_pnl(self, db: AsyncSession, user: User) -> Decimal:
        """每日盈亏：以午夜总资产为基线。"""
        from app.ai.strategy_gates import calc_daily_pnl
        return await calc_daily_pnl(db, user.id)

    async def _count_active_bets(self, db: AsyncSession, user: User) -> int:
        """统计当前未结算的成功注单数（真实持仓，用于仓位风险折扣）。"""
        result = await db.execute(
            select(func.count(Bet.id)).where(
                Bet.user_id == user.id,
                Bet.status == BetStatus.SUCCESS,
                Bet.settled_at.is_(None),
            )
        )
        return result.scalar_one()

    async def _match_already_bet(self, db: AsyncSession, user_id: int, match_id: int) -> bool:
        """检查今天是否已对该比赛下过注（防止跨轮次重复下单）。

        同时检查 DB（已确认注单）和 Redis（OB 返回了 orderNo 但验证未通过的待定注单）。
        """
        # 1. 检查 DB 已确认注单
        today = datetime.now(timezone.utc).date()
        today_start = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)
        result = await db.execute(
            select(func.count(Bet.id)).where(
                Bet.user_id == user_id,
                Bet.is_ai_bet.is_(True),
                Bet.match_id == match_id,
                Bet.created_at >= today_start,
            )
        )
        if int(result.scalar_one() or 0) > 0:
            return True

        # 2. 检查 Redis 待定注单（OB 返回 orderNo 但验证未通过）
        try:
            from app.core.cache import cache
            val = await cache.get_json(f"ai:bet:pending:{user_id}:{match_id}")
            if val and val.get("order_no"):
                return True
        except Exception:
            pass
        return False

    async def _mark_bet_pending(
        self,
        match_id: int,
        order_no: str,
        *,
        selection: str = "",
        bet_type: str = "",
        odds: float | None = None,
        stake: Decimal | float | None = None,
        line: float | None = None,
        confidence: float | None = None,
        reasoning: str = "",
        provider: str = "",
    ) -> None:
        """记录 OB 返回了 orderNo 的待定注单（TTL 6 小时，防止跨轮次重复下单）。"""
        try:
            from app.core.cache import cache
            # 记录 match_id + orderNo，TTL 6h（覆盖一个比赛日）
            payload = {
                "order_no": order_no,
                "time": datetime.now(timezone.utc).isoformat(),
            }
            if selection:
                payload["selection"] = str(selection)
            if bet_type:
                payload["bet_type"] = str(bet_type)
            if odds is not None:
                payload["odds"] = float(odds)
            if stake is not None:
                payload["stake"] = float(stake)
            if line is not None:
                payload["line"] = float(line)
            if confidence is not None:
                payload["confidence"] = float(confidence)
            if reasoning:
                payload["reasoning"] = str(reasoning)
            if provider:
                payload["provider"] = str(provider)
            await cache.set_json(
                f"ai:bet:pending:{self.user_id}:{match_id}",
                payload,
                ttl=21600,
            )
            logger.info("已记录待定注单 match=%s orderNo=%s（防重复 6h）", match_id, order_no)
        except Exception as e:
            logger.warning("记录待定注单失败: %s", e)

    async def _notify(self, user_id: int, event_type: str, data: dict):
        """推送通知"""
        await manager.broadcast_to_user(user_id, {
            "type": f"ai_{event_type}",
            "data": data,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })


# === 单场即时分析 API ===
async def analyze_and_recommend(
    match_id: int,
    user_id: int,
    *,
    stake: float | None = None,
    shared_analysis: dict | None = None,
    shared_ctx: dict | None = None,
    skip_match_context: bool | None = None,
    strat_override=None,
) -> dict:
    """
    AI 推荐：OB / 平博单边 · 足球胜负/让球/大小 · 篮球全场大小。

    shared_analysis / shared_ctx：跨站同场复用，跳过 LLM（分析结果共享）。
    注意：LLM 可能耗时数十秒，必须先释放 DB 连接，避免 idle_in_transaction 杀连接。
    """
    from app.ai.market_recommend import build_match_market_recommendations, SEL_LABELS
    from app.models.user import BookmakerAccount
    from app.services.bookmakers.catalog import provider_name as _pname
    from app.services.fixture_key import fixture_key_for_match
    from app.services.provider_utils import site_code_from_match

    providers_filter = set(SINGLE_SIDE_PROVIDER_NAMES)
    engine = AIBettingEngine(user_id)

    # --- Phase 1: 短事务加载赛事 + 用户 AI 策略 ---
    async with AsyncSessionLocal() as db:
        # 手动分析同样只看已连接站点赔率（未连接站的赔率选不中、下不了单）
        try:
            _conn_names = await connected_provider_names(db, user_id)
            if _conn_names:
                providers_filter = _conn_names
        except Exception:
            pass
        match_result = await db.execute(select(Match).where(Match.id == match_id))
        match = match_result.scalar_one_or_none()
        if not match:
            return {"error": "赛事不存在"}

        # 分析时也热加载，保证推荐页与自动投注用同一套最新阈值
        if strat_override is not None:
            strat_cfg = strat_override
            ai_config = (await db.execute(
                select(AIConfig).where(AIConfig.user_id == user_id)
                .execution_options(populate_existing=True)
            )).scalar_one_or_none()
        else:
            ai_config, strat_cfg = await load_fresh_strategy(user_id)
        user_engine = StrategyEngine(strat_cfg, user_id=user_id)

        from app.ai.strategy_gates import sport_is_preferred, team_is_excluded

        sport_key = match.sport.value if hasattr(match.sport, "value") else str(match.sport)
        preferred = list(getattr(ai_config, "preferred_sports", None) or [])
        if not sport_is_preferred(sport_key, preferred):
            await db.commit()
            return {
                "error": "球类不在偏好列表中",
                "match_id": match_id,
                "recommendation": {"should_bet": False, "reasoning": "[不投注] 球类不在配置偏好中"},
            }
        if is_china_match(
            match.league or "",
            match.home_team or "",
            match.away_team or "",
            sport_key,
        ):
            await db.commit()
            return {
                "error": "中国赛事已过滤",
                "match_id": match_id,
                "recommendation": {"should_bet": False, "reasoning": "[不投注] 中国赛事不分析不下单"},
            }
        excluded = list(getattr(ai_config, "excluded_teams", None) or [])
        if team_is_excluded(match.home_team or "", match.away_team or "", excluded):
            await db.commit()
            return {
                "error": "球队在排除名单中",
                "match_id": match_id,
                "recommendation": {"should_bet": False, "reasoning": "[不投注] 球队在排除名单中"},
            }
        match_status = match.status.value if hasattr(match.status, "value") else str(match.status)
        site_hint = site_code_from_match(match) or ""
        fk = fixture_key_for_match(match)
        match_info = {
            "id": match.id,
            "match_id": match.id,
            "sport": sport_key,
            "league": match.league,
            "home_team": match.home_team,
            "away_team": match.away_team,
            "start_time": match.start_time.isoformat() if match.start_time else "",
            "venue": match.venue,
            "provider_code": site_hint,
            "fixture_key": fk,
            "extra_data": dict(match.extra_data or {}) if isinstance(match.extra_data, dict) else {},
        }
        # 足球：胜负/让球/大小；篮球：大小
        from app.ai.market_recommend import load_all_market_odds_pack

        odds_pack = await load_all_market_odds_pack(
            db, match_id, sport_key, providers_filter=providers_filter
        )
        markets_odds = odds_pack.get("markets") or {}
        odds = dict(odds_pack.get("flat") or {})
        total_line = None
        spread_line = None
        if isinstance(markets_odds.get("total"), dict):
            total_line = markets_odds["total"].get("line")
            odds.update(markets_odds["total"].get("odds") or {})
        if isinstance(markets_odds.get("spread"), dict):
            spread_line = markets_odds["spread"].get("line")
        match_info["odds"] = {
            "markets": markets_odds,
            "line_movements": odds_pack.get("line_movements") or {},
            "odds_style": "asian",
            **odds,
        }
        match_info["total_line"] = total_line
        match_info["spread_line"] = spread_line
        match_info["line"] = total_line if total_line is not None else spread_line
        match_info["line_movements"] = odds_pack.get("line_movements") or {}
        try:
            match_info["home_score"] = getattr(match, "home_score", None)
            match_info["away_score"] = getattr(match, "away_score", None)
            extra = getattr(match, "extra_data", None) or {}
            if isinstance(extra, dict):
                match_info["period"] = extra.get("period") or extra.get("status_text")
                match_info["clock"] = extra.get("clock") or extra.get("timer") or extra.get("time")
        except Exception:
            pass

        from app.ai.analysis_filters import skip_reason_for_match

        skip_why = skip_reason_for_match(
            match,
            total_line,
            odds_map={"markets": markets_odds, **odds},
        )
        if skip_why:
            await db.commit()
            reason_map = {
                "score_exceeds_line": "当前比分已超过小球盘口，跳过分析",
                "ending_soon": "比赛预计 10 分钟内结束，跳过分析",
                "china_match": "中国赛事已过滤，跳过分析",
            }
            msg = reason_map.get(skip_why, f"跳过分析: {skip_why}")
            return {
                "error": msg,
                "match_id": match_id,
                "home_team": match_info.get("home_team"),
                "away_team": match_info.get("away_team"),
                "league": match_info.get("league"),
                "sport": sport_key.lower() if hasattr(sport_key, "lower") else str(sport_key).lower(),
                "home_score": match_info.get("home_score"),
                "away_score": match_info.get("away_score"),
                "period": match_info.get("period"),
                "clock": match_info.get("clock"),
                "total_line": total_line,
                "skip_reason": skip_why,
                "recommendation": {"should_bet": False, "reasoning": f"[不投注] {msg}"},
            }

        await db.commit()

    # --- Phase 2: 赛前上下文 + LLM（受 use_llm_analysis 控制）---
    from app.ai.match_context import fetch_match_context, fetch_match_context_fast

    use_llm = bool(strat_cfg.use_llm_analysis)
    if shared_analysis is not None:
        analysis = dict(shared_analysis)
        ctx = dict(shared_ctx or {})
        match_info["recent_form"] = {
            "home": ctx.get("home_form") or {},
            "away": ctx.get("away_form") or {},
        }
        match_info["context_source"] = ctx.get("source") or "shared"
        llm_down = bool(analysis.get("error")) or not analysis.get("models_used")
        conf0 = float(analysis.get("confidence") or 0.5)
    elif not use_llm:
        ctx = {}
        match_info["recent_form"] = {"home": {}, "away": {}}
        match_info["context_source"] = "strategy_market"
        llm_down = True
        conf0 = 0.0
        analysis = {
            "prediction": "",
            "confidence": 0.0,
            "reasoning": "[不投注] AI分析已关闭，仅允许展示，不允许自动下单",
            "models_used": [],
            "consensus_reached": False,
            "should_bet": False,
            "error": None,
        }
    else:
        ctx: dict = {}
        # skip_match_context=True 时只走缓存快路径；否则允许短超时补抓 nowscore 基本面
        if skip_match_context:
            try:
                ctx = await asyncio.wait_for(fetch_match_context_fast(match_info), timeout=3.0)
            except asyncio.TimeoutError:
                logger.warning("match_context fast fetch timeout, skip")
                ctx = {}
            except Exception as e:
                logger.warning("fetch_match_context_fast failed match=%s: %s", match_id, e)
                ctx = {}
        else:
            try:
                # 实时分析优先读 Redis / DB，未命中时允许短超时直抓 nowscore 以补全基本面。
                ctx = await asyncio.wait_for(fetch_match_context(match_info, refresh_on_miss=True), timeout=8.0)
            except asyncio.TimeoutError:
                logger.warning("match_context fetch timeout, fallback to cache-only match=%s", match_id)
                try:
                    ctx = await asyncio.wait_for(fetch_match_context_fast(match_info), timeout=2.0)
                except Exception:
                    ctx = {}
            except Exception as e:
                logger.warning("fetch_match_context failed match=%s: %s", match_id, e)
                try:
                    ctx = await asyncio.wait_for(fetch_match_context_fast(match_info), timeout=2.0)
                except Exception:
                    ctx = {}

        historical_pack = {
            "h2h": ctx.get("h2h") or {},
            "home_form": ctx.get("home_form") or {},
            "away_form": ctx.get("away_form") or {},
            "standings": ctx.get("standings") or {},
            "analysis": ctx.get("analysis") or {},
            "trend": ctx.get("trend") or {},
            "venue": ctx.get("venue") or "",
            "dimensions_present": ctx.get("dimensions_present") or [],
            "dimensions_missing": ctx.get("dimensions_missing") or [],
            "quality": ctx.get("quality") or {},
            "source": ctx.get("source") or "none",
            "model_used": ctx.get("model_used"),
            "fetched_at": ctx.get("fetched_at"),
        }
        match_info["recent_form"] = {
            "home": ctx.get("home_form") or {},
            "away": ctx.get("away_form") or {},
        }
        match_info["context_source"] = ctx.get("source") or "none"

        analysis = await analyzer.analyze_match(
            match_info,
            historical_data=historical_pack,
            market_odds={
                "markets": markets_odds,
                "line_movements": odds_pack.get("line_movements") or {},
                "odds_style": "asian",
                **odds,
            },
        )
        llm_down = bool(analysis.get("error")) or not analysis.get("models_used")
        analysis["llm_fallback"] = bool(llm_down)
        conf0 = float(analysis.get("confidence") or 0.5)
        ctx_bits = []
        h2n = len((ctx.get("h2h") or {}).get("matches") or [])
        hf = len((ctx.get("home_form") or {}).get("matches") or [])
        af = len((ctx.get("away_form") or {}).get("matches") or [])
        st = 1 if (ctx.get("standings") or {}).get("home") or (ctx.get("standings") or {}).get("away") else 0
        src = str(ctx.get("source") or "none")
        if h2n or hf or af or st:
            ctx_bits.append(f"交锋{h2n}/主近况{hf}/客近况{af}/积分{st}(源:{src})")
        else:
            ctx_bits.append(f"交锋/近10场/积分:暂无数据(源:{src})")
        ctx_note = "[" + " | ".join(ctx_bits) + "] "

        err = str(analysis.get("error") or "")
        quota_hit = any(
            x in (err + str(analysis.get("reasoning") or "")).lower()
            for x in ("quota", "额度", "rate limit", "429", "exhausted")
        )

        if llm_down:
            # 真正无可用模型 / 超时：可展示盘口启发式，但禁止放行共识门禁
            why = "LLM 暂不可用"
            if "timeout" in err.lower():
                why = "LLM 分析超时"
            elif quota_hit:
                why = "模型额度耗尽/未开通后付费"
            analysis = {
                **analysis,
                "confidence": min(float(conf0 or 0), 0.49),
                "consensus_reached": False,
                "should_bet": False,
                "reasoning": (
                    f"[盘口启发式·不可下单] {why}，共识未通过，仅供参考。 "
                    + ctx_note
                    + str(analysis.get("reasoning") or "")
                )[:500],
            }
        elif analysis.get("consensus_reached") is False:
            # 有模型返回但共识不足：不得伪造 consensus
            analysis = {
                **analysis,
                "should_bet": False,
                "reasoning": (
                    "[共识不足·不可下单] 模型未达共识门槛，禁止放行。 "
                    + ctx_note
                    + str(analysis.get("reasoning") or "")
                )[:500],
            }
        elif ctx_note and analysis.get("reasoning"):
            analysis = {
                **analysis,
                "reasoning": (ctx_note + str(analysis.get("reasoning") or ""))[:500],
            }
    # --- Phase 3: 新事务加载盘口 + 按 AI 策略决策 ---
    max_amt = Decimal(str(strat_cfg.max_bet_amount))
    default_stake = min(Decimal(str(stake)) if stake is not None else max_amt, max_amt)
    async with AsyncSessionLocal() as db:
        conf_use = float(analysis.get("confidence") or conf0 or 0)
        conf_use = max(0.0, min(0.99, conf_use))
        pred = str(analysis.get("prediction") or "").lower().strip()
        if pred != "under":
            pred = ""

        markets = await build_match_market_recommendations(
            db,
            match_id=match_id,
            sport=sport_key,
            prediction=pred,
            confidence=conf_use if conf_use > 0 else conf0,
            stake=default_stake if default_stake >= settings.AI_MIN_BALANCE else max_amt,
            providers_filter=providers_filter,
            min_odds=1.01,
            max_odds=None,
            preferred_bet_type="total",
        )
        primary_market = next(
            (m for m in markets if str(m.get("bet_type") or "") == "total"),
            None,
        )
        primary = dict((primary_market or {}).get("single") or {})
        bet_type = str(
            (primary_market or {}).get("bet_type")
            or primary.get("bet_type")
            or "total"
        ).lower()

        provider_code = str(primary.get("provider_code") or site_hint or "pinnacle").lower()
        if provider_code not in SINGLE_SIDE_PROVIDER_CODES:
            provider_code = "pinnacle"
        provider_name_str = _pname(provider_code)
        if primary:
            primary = {
                **primary,
                "provider": primary.get("provider") or provider_name_str,
                "provider_code": provider_code,
            }
            provider_name_str = str(primary.get("provider") or provider_name_str)

        mkt_pack = await engine._get_best_market_pack(
            db, match_id, bet_type, providers_filter=providers_filter
        )
        market_odds_flat = dict(mkt_pack.get("odds") or {})
        for cell in (primary_market or {}).get("cells") or []:
            if cell.get("available") and cell.get("odds") and cell.get("selection"):
                market_odds_flat.setdefault(str(cell["selection"]), float(cell["odds"]))

        analysis_pick_allowed = bool(use_llm) and pred == "under"
        consensus_ok = True if not use_llm else bool(analysis.get("consensus_reached", True))
        if analysis_pick_allowed:
            sel = str(primary.get("selection") or pred or "").lower()
        else:
            sel = pred
        if not consensus_ok and not analysis_pick_allowed:
            sel = pred
        sel_label = primary.get("selection_label") if analysis_pick_allowed else ""
        sel_label = sel_label or SEL_LABELS.get(sel, sel)
        sel_odds = float(primary.get("odds") or market_odds_flat.get(sel) or 0) if analysis_pick_allowed else 0.0
        try:
            wr = float(primary.get("win_rate"))
            if wr > 1:
                conf_use = max(0.01, min(0.99, wr / 100.0))
            elif wr > 0:
                conf_use = max(0.01, min(0.99, wr))
        except (TypeError, ValueError):
            pass
        if analysis_pick_allowed and sel == "under":
            analysis = {
                **analysis,
                "prediction": sel,
                "bet_type": bet_type,
                "confidence": conf_use,
            }
            pred = sel
        line = (primary_market or {}).get("line")
        if line is None:
            line = mkt_pack.get("line") or match_info.get("total_line") or match_info.get("spread_line")

        site_bal = Decimal("0")
        acc_res = await db.execute(
            select(BookmakerAccount).where(
                BookmakerAccount.user_id == user_id,
                BookmakerAccount.code == provider_code,
            )
        )
        acc = acc_res.scalar_one_or_none()
        if acc:
            site_bal = Decimal(str(acc.balance or 0))
        balance = site_bal if site_bal > 0 else max_amt

        # Risk calculations deferred to Phase 3 (only needed for evaluate_bet)
        daily_loss_amt = Decimal("0")
        active_count = 0
        try:
            urow = await db.get(User, user_id)
            if urow:
                pnl = await engine._calc_daily_pnl(db, urow)
                daily_loss_amt = abs(pnl) if pnl < 0 else Decimal("0")
                active_count = await engine._count_active_bets(db, urow)
        except Exception:
            pass

        if (
            not analysis_pick_allowed
            or not primary
            or sel != "under"
            or sel_odds <= 1
        ):
            decision = BetDecision(
                match_id=match_id,
                selection=sel or "skip",
                confidence=conf_use,
                suggested_stake=Decimal("0"),
                reasoning=(
                    str(analysis.get("reasoning") or "[不投注] AI 未给出可下单方向")
                    if not analysis_pick_allowed
                    else "[不投注] 无可用盘口"
                ),
                risk_score=1.0,
                should_bet=False,
                bet_type=bet_type,
                provider_code=provider_code,
                odds=sel_odds,
                line=float(line) if line is not None else None,
            )
        else:
            primary["win_rate"] = round(conf_use * 100, 1)
            decision = await user_engine.evaluate_bet(
                match_info={
                    **match_info,
                    "odds": market_odds_flat,
                    "provider_code": provider_code,
                    "bet_type": bet_type,
                    "line_movement": (primary_market or {}).get("line_movement"),
                    "line_movements": odds_pack.get("line_movements") or {},
                },
                analysis={
                    **analysis,
                    "prediction": sel,
                    "bet_type": bet_type,
                    "confidence": conf_use,
                    "odds": sel_odds,
                    "provider_code": provider_code,
                    "line": line,
                    "consensus_reached": consensus_ok,
                },
                user_balance=balance,
                daily_loss=daily_loss_amt,
                active_bets_count=active_count,
            )

        from app.ai.strategy_gates import check_daily_risk

        # 仓位：用户显式指定金额 > 策略动态仓位；封顶单笔上限；拒绝单归零
        user_stake = Decimal(str(stake)) if stake is not None and Decimal(str(stake)) > 0 else None
        if user_stake is not None:
            sug_stake = user_stake
        else:
            sug_stake = Decimal(str(decision.suggested_stake or 0))
        if decision.should_bet:
            if sug_stake <= 0:
                # 异常仓位按最小注兜底（展示用途；执行层仍会拒绝 ≤0 的单）
                sug_stake = Decimal("1")
            sug_stake = min(sug_stake, Decimal(str(strat_cfg.max_bet_amount or 1)))
            if sug_stake < Decimal("1"):
                sug_stake = Decimal("1")
            decision = decision.model_copy(update={"suggested_stake": sug_stake})
        else:
            sug_stake = Decimal("0")
        if decision.should_bet:
            risk_hit, risk_why = await check_daily_risk(db, user_id, strat_cfg)
            if risk_hit:
                decision = decision.model_copy(
                    update={
                        "should_bet": False,
                        "suggested_stake": Decimal("0"),
                        "reasoning": f"[不投注] {risk_why}",
                    }
                )
                sug_stake = Decimal("0")
        win_rate = float(
            primary.get("win_rate")
            if primary.get("win_rate") is not None
            else round(float(decision.confidence or conf_use) * 100, 1)
        )
        raw_confidence = float(decision.confidence or conf_use or 0)
        raw_win_rate = float(win_rate if win_rate > 0 else round(raw_confidence * 100, 1))
        raw_odds = float(sel_odds or decision.odds or 0)

        context_meta = {
            "source": ctx.get("source") or "none",
            "model_used": ctx.get("model_used"),
            "fetched_at": ctx.get("fetched_at"),
            "cache_hit": bool(ctx.get("cache_hit")),
            "h2h_count": len((ctx.get("h2h") or {}).get("matches") or []),
            "home_form_count": len((ctx.get("home_form") or {}).get("matches") or []),
            "away_form_count": len((ctx.get("away_form") or {}).get("matches") or []),
            "standings_count": 1 if (ctx.get("standings") or {}).get("home") or (ctx.get("standings") or {}).get("away") else 0,
            "note": (ctx.get("note") or (ctx.get("h2h") or {}).get("note") or "")[:120],
        }

        return {
            "match_id": match_id,
            "home_team": match_info["home_team"],
            "away_team": match_info["away_team"],
            "league": match_info["league"],
            "sport": sport_key.lower() if hasattr(sport_key, "lower") else str(sport_key).lower(),
            "home_score": match_info.get("home_score"),
            "away_score": match_info.get("away_score"),
            "period": match_info.get("period"),
            "clock": match_info.get("clock"),
            "total_line": line if line is not None else total_line,
            "fixture_key": fk,
            "analysis_shared": bool(shared_analysis is not None),
            "analysis": analysis,
            "context_meta": context_meta,
            "markets": markets,
            "line_movements": odds_pack.get("line_movements") or {},
            "recommendation": {
                "should_bet": bool(decision.should_bet and primary and analysis_pick_allowed),
                "selection": sel,
                "selection_label": sel_label,
                "confidence": raw_confidence,
                "win_rate": raw_win_rate,
                "raw_confidence": raw_confidence,
                "raw_win_rate": raw_win_rate,
                "suggested_stake": float(sug_stake),
                "reasoning": decision.reasoning,
                "risk_score": decision.risk_score,
                "analysis_pick_allowed": bool(analysis_pick_allowed),
                "provider": (primary.get("provider") or provider_name_str) if analysis_pick_allowed else "",
                "provider_code": (primary.get("provider_code") or provider_code) if analysis_pick_allowed else "",
                "odds": raw_odds,
                "raw_odds": raw_odds,
                "bet_type": bet_type,
                "line": line,
                "llm_fallback": bool(llm_down),
            },
            "strategy": {
                "name": strat_cfg.name,
                "max_bet_amount": strat_cfg.max_bet_amount,
                "max_daily_bets": strat_cfg.max_daily_bets,
                "stop_loss": strat_cfg.stop_loss,
                "take_profit": strat_cfg.take_profit,
                "use_llm_analysis": strat_cfg.use_llm_analysis,
            },
            "current_odds": {
                k: v for k, v in (market_odds_flat or {}).items()
                if k == "under" and isinstance(v, (int, float))
            },
            "best_by_selection": mkt_pack.get("best_by_selection") or {},
            "odds_by_provider": mkt_pack.get("odds_by_provider") or {},
            "site_scope": "ob,pinnacle",
            "market_scope": "total",
            "match_status": match_status,
        }


async def analyze_fixture_group(
    match_ids: list[int],
    user_id: int,
    *,
    stake: float | None = None,
    skip_match_context: bool | None = None,
    strat_override: Optional[StrategyConfig] = None,
) -> list[dict]:
    """
    同场多站点（OB/平博）只跑一次 LLM，盘口/下单决策按各站 match_id 分别生成。
    返回每站一条推荐（含共享 analysis）；调用方可再按站点过滤或择优只留一单。
    """
    from app.services.fixture_key import pick_canonical_match

    ids = [int(x) for x in match_ids if x]
    if not ids:
        return []

    async with AsyncSessionLocal() as db:
        rows = list(
            (await db.execute(select(Match).where(Match.id.in_(ids)))).scalars().all()
        )
        await db.commit()
    if not rows:
        return []

    by_id = {int(m.id): m for m in rows}
    ordered = [by_id[i] for i in ids if i in by_id]
    if not ordered:
        return []

    # 批量默认跳过赛前上下文；单场引擎可显式 False 打开
    if skip_match_context is None:
        skip_match_context = not bool(
            getattr(settings, "AI_MATCH_CONTEXT_IN_BATCH", False)
        )
    # 使用传入的策略缓存，避免每个 fixture group 重复查 DB
    if strat_override is not None:
        strat_cfg = strat_override
    else:
        _, strat_cfg = await load_fresh_strategy(user_id)
    canonical = pick_canonical_match(ordered)
    primary = await analyze_and_recommend(
        int(canonical.id),
        user_id,
        stake=stake,
        skip_match_context=bool(skip_match_context),
        strat_override=strat_cfg,
    )
    if primary.get("error"):
        return []

    analysis = primary.get("analysis") or {}
    meta = primary.get("context_meta") or {}
    shared_ctx = {
        "source": meta.get("source") or "shared",
        "model_used": meta.get("model_used"),
        "fetched_at": meta.get("fetched_at"),
        "cache_hit": True,
        "h2h": {},
        "home_form": {},
        "away_form": {},
        "note": meta.get("note") or "",
    }
    sibling_ids = [int(m.id) for m in ordered]
    primary["sibling_match_ids"] = sibling_ids
    primary["analysis_shared"] = False

    out = [primary]
    sibling_tasks = []
    for m in ordered:
        mid = int(m.id)
        if mid == int(canonical.id):
            continue
        sibling_tasks.append(analyze_and_recommend(
            mid,
            user_id,
            stake=stake,
            shared_analysis=analysis,
            shared_ctx=shared_ctx,
            strat_override=strat_cfg,
        ))
    if sibling_tasks:
        sibling_results = await asyncio.gather(*sibling_tasks, return_exceptions=True)
        for r in sibling_results:
            if isinstance(r, dict) and r and not r.get("error"):
                r["sibling_match_ids"] = sibling_ids
                r["analysis_shared"] = True
                out.append(r)
    return out


def pick_best_rec_for_fixture(recs: list[dict]) -> Optional[dict]:
    """同场多站推荐中择优一单（优先可投 + 胜率）。"""
    if not recs:
        return None
    return max(
        recs,
        key=lambda r: (
            1 if (r.get("recommendation") or {}).get("should_bet") else 0,
            float((r.get("recommendation") or {}).get("confidence") or 0),
            float((r.get("recommendation") or {}).get("odds") or 0),
        ),
    )


# === 全局引擎管理 ===
_active_engines: dict[int, AIBettingEngine] = {}


_ENGINE_LOCK_TTL = 900  # 引擎锁/运行标记 TTL：进程崩溃后最多 15 分钟自动过期


def _engine_lock_key(user_id: int) -> str:
    return f"ai:engine:lock:{user_id}"


def _engine_running_key(user_id: int) -> str:
    return f"ai:engine:running:{user_id}"


async def _clear_stale_engine_lock(user_id: int, lock_key: str) -> bool:
    """仅在没有运行心跳时，原子删除遗留的引擎锁。"""
    from app.core.cache import cache

    running_key = _engine_running_key(user_id)
    try:
        owner = await cache.get(lock_key)
        if not owner or await cache.exists(running_key):
            return False
        lua = """
        if redis.call('get', KEYS[1]) == ARGV[1] and redis.call('exists', KEYS[2]) == 0 then
            return redis.call('del', KEYS[1])
        end
        return 0
        """
        return bool(await cache.client.eval(lua, 2, lock_key, running_key, owner))
    except Exception as e:
        logger.warning("AI 引擎失效锁清理失败: user=%s error=%s", user_id, e)
        return False


async def start_user_engine(user_id: int) -> dict:
    """为用户启动AI引擎。一律真实下单，并由 bet_mode 控制是否自动执行。

    跨进程互斥：Redis SET NX 锁防止多 worker 同时为同一用户跑引擎
    （否则双引擎并发扫描会导致重复下单）。
    """
    import uuid as _uuid

    from app.core.cache import cache
    from app.core.worker_leader import worker_id

    lock_key = _engine_lock_key(user_id)
    token = f"{worker_id()}:{_uuid.uuid4().hex[:8]}"
    recovered_stale_lock = False
    try:
        got = await cache.client.set(lock_key, token, nx=True, ex=_ENGINE_LOCK_TTL)
        if not got and await _clear_stale_engine_lock(user_id, lock_key):
            recovered_stale_lock = True
            got = await cache.client.set(lock_key, token, nx=True, ex=_ENGINE_LOCK_TTL)
    except Exception:
        got = True  # Redis 不可用时降级为单进程内存互斥
    if not got:
        logger.warning("AI 引擎启动被拒：用户 %s 的引擎已在其他实例运行", user_id)
        return {"status": "already_running", "user_id": user_id, "bet_mode_gated": True}

    if user_id in _active_engines:
        await _active_engines[user_id].stop()
        del _active_engines[user_id]

    engine = AIBettingEngine(user_id)
    engine._engine_lock_token = token
    _active_engines[user_id] = engine
    try:
        # 启动即写入心跳，避免首轮分析尚未完成时只有锁、没有运行状态。
        await cache.set(_engine_running_key(user_id), "1", ttl=_ENGINE_LOCK_TTL)
    except Exception as e:
        # Redis 不可用时保留本地引擎，避免降级模式下无法启动。
        logger.warning("AI 引擎运行心跳写入失败: user=%s error=%s", user_id, e)
    try:
        await engine.start()
    except Exception:
        _active_engines.pop(user_id, None)
        try:
            await cache.delete(_engine_running_key(user_id))
            await cache.release_lock(lock_key, token)
        except Exception:
            pass
        raise

    return {
        "status": "recovered_stale_lock" if recovered_stale_lock else "started",
        "user_id": user_id,
        "bet_mode_gated": True,
    }


async def stop_user_engine(user_id: int) -> dict:
    """停止用户AI引擎"""
    from app.core.cache import cache

    if user_id in _active_engines:
        engine = _active_engines[user_id]
        await engine.stop()
        del _active_engines[user_id]
        # 只释放本实例持有的锁（token 校验，Lua 保证原子性）
        token = getattr(engine, "_engine_lock_token", "")
        if token:
            try:
                await cache.release_lock(_engine_lock_key(user_id), token)
            except Exception:
                pass
    # 清除 Redis 标记
    try:
        await cache.delete(_engine_running_key(user_id))
    except Exception:
        pass
    return {"status": "stopped", "user_id": user_id}


async def stop_local_user_engines() -> None:
    """停止当前 worker 持有的引擎，供优雅退出时释放 Redis 状态。"""
    for user_id in list(_active_engines):
        try:
            await stop_user_engine(user_id)
        except Exception:
            logger.exception("AI 引擎退出清理失败: user=%s", user_id)


async def get_engine_status(user_id: int) -> dict:
    """获取引擎状态（跨 worker：内存优先，Redis 兜底）"""
    engine = _active_engines.get(user_id)
    if engine and engine.is_running:
        return {"running": True}
    # 当前 worker 无引擎，查 Redis 看是否在其他 worker 运行（TTL 兜底防僵尸标记）
    try:
        from app.core.cache import cache
        val = await cache.get(_engine_running_key(user_id))
        if val and str(val) in ("1", "true", "True"):
            return {"running": True}
    except Exception:
        pass
    return {"running": False}
