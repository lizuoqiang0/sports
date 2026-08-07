"""
AI 自动投注引擎 - 主调度器

工作流程:
1. 定时扫描 OB / 平博 各自滚球（足球/篮球）赛事
2. 用 LLM + 盘口矩阵分析全场大小球
3. 策略引擎评估投注决策
4. 自动执行真实下单（人工模式仅推荐；每轮最多 N 单、不同比赛）
5. 风控监控 (止损/止盈/限额)
6. 记录 AI 决策日志
"""
import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, func

from app.database import AsyncSessionLocal, get_db
from app.models.user import (
    User, AIConfig, Match, MatchStatus, Bet, BetStatus, BetType,
    Transaction, TransactionType, BookmakerAccount, BookmakerStatus, Odds,
)
from app.ai.analyzer import analyzer
from app.ai.strategy import (
    BetDecision,
    StrategyEngine,
    decision_passes_strategy,
    effective_strategy_from_ai_config,
    load_fresh_strategy,
    strategy_engine,
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
# 每轮最多分析的滚球场次（按站点+球类覆盖）
_LIVE_SCAN_LIMIT = max(
    60, int(getattr(settings, "AI_LIVE_SCAN_LIMIT", 120) or 120)
)
_LIVE_ANALYZE_CONCURRENCY = max(
    2, int(getattr(settings, "ENSEMBLE_CONCURRENCY", 8) or 8)
)


def _decision_profit_score(d: BetDecision) -> tuple:
    """
    利益最大化排序键（越大越优先）：
    1) 期望利润 = 仓位 × (胜率×赔率 - 1)
    2) 期望边际 EV = 胜率×赔率 - 1
    3) 胜率（置信度）
    4) 赔率
    """
    conf = float(getattr(d, "confidence", 0) or 0)
    if conf > 1.0:
        conf = conf / 100.0
    conf = max(0.0, min(0.99, conf))
    try:
        odds = float(getattr(d, "odds", 0) or 0)
    except (TypeError, ValueError):
        odds = 0.0
    edge = (conf * odds - 1.0) if odds > 1.0 else -1.0
    # 若决策里已有 EV，取两者较大值，避免被偏低缓存 EV 误伤
    try:
        stored_ev = float(getattr(d, "expected_value", 0) or 0)
    except (TypeError, ValueError):
        stored_ev = 0.0
    edge = max(edge, stored_ev)
    try:
        stake = float(getattr(d, "suggested_stake", 0) or 0)
    except (TypeError, ValueError):
        stake = 0.0
    exp_profit = edge * stake if stake > 0 else edge
    return (exp_profit, edge, conf, odds)


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
        """主循环：分析/下单一轮后休眠（默认 30 分钟）"""
        while self.is_running:
            try:
                await self._run_cycle()
                # 每轮间隔：默认 600s=10 分钟（热读 settings，改 .env 后重启生效）
                interval = max(60, int(getattr(settings, "AI_SCAN_INTERVAL_SEC", 600) or 600))
                logger.info("AI 引擎本轮结束，%s 秒后下一轮", interval)
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"AI引擎异常: {e}", exc_info=True)
                await asyncio.sleep(settings.AI_RETRY_SLEEP_SEC)

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
        remaining_daily = 0
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
            if not ai_config or not getattr(ai_config, "is_active", True):
                self.is_running = False
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
                logger.info("人工模式：本轮仅分析推荐，不自动下单")

            candidates = await self._scan_candidates(db, ai_config)
            if not candidates:
                logger.debug("本轮无滚球候选赛事")
                return

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
                "本轮策略 user=%s conf>=%.0f%% odds=[%.2f,%.2f] stake<=%s daily<=%s stop=%s tp=%s",
                self.user_id,
                strat_cfg.min_confidence * 100,
                strat_cfg.min_odds,
                strat_cfg.max_odds,
                strat_cfg.max_bet_amount,
                strat_cfg.max_daily_bets,
                strat_cfg.stop_loss,
                strat_cfg.take_profit,
            )
            daily_loss = await self._calc_daily_pnl(db, user)
            daily_loss_amt = abs(daily_loss) if daily_loss < 0 else Decimal("0")
            active_count = await self._count_active_bets(db, user)
            today_count = await self._count_today_bets(db, user)
            remaining_daily = max(0, int(strat_cfg.max_daily_bets) - int(today_count))
            if remaining_daily <= 0:
                logger.info(
                    "已达每日投注上限: %s/%s",
                    today_count,
                    strat_cfg.max_daily_bets,
                )
                return
            spendable = await self._spendable_balance(db, user)
            if spendable < settings.AI_MIN_BALANCE:
                logger.info("可用余额不足: %s", spendable)
                return
            await db.commit()
        # Phase 1 结束：连接已释放，再跑 LLM

        # --- Phase 2: LLM 分析（无 DB）---
        user_engine = StrategyEngine(strat_cfg)
        strategy_engine.config = strat_cfg
        uid = self.user_id
        sem = asyncio.Semaphore(_LIVE_ANALYZE_CONCURRENCY)
        analyses: list[dict] = []

        async def _analyze_group(ids: list[int]) -> Optional[dict]:
            async with sem:
                try:
                    recs = await analyze_fixture_group(ids, uid)
                    return pick_best_rec_for_fixture(recs)
                except Exception as e:
                    logger.warning("滚球同场分析失败 ids=%s: %s", ids, e)
                    return None

        raw = await asyncio.gather(*[_analyze_group(g) for g in fixture_groups])
        for item in raw:
            if item and not item.get("error"):
                analyses.append(item)

        decisions: list[BetDecision] = []
        for item in analyses:
            rec = item.get("recommendation") or {}
            sel = str(rec.get("selection") or "").lower()
            bt = str(rec.get("bet_type") or (item.get("analysis") or {}).get("bet_type") or "total").lower()
            allowed = {
                "total": {"over", "under"},
                "moneyline": {"home", "away", "draw"},
                "spread": {"home", "away"},
            }
            if sel not in allowed.get(bt, ()):
                continue
            odds_map = dict(item.get("current_odds") or {})
            if rec.get("odds"):
                odds_map[sel] = float(rec["odds"])
            conf = float(rec.get("confidence") or (item.get("analysis") or {}).get("confidence") or 0)
            # EV: 优先用分析结果的去 vig EV，其次用推荐结果
            analysis_ev = float((item.get("analysis") or {}).get("expected_value") or 0)
            mkt_ev = float(rec.get("expected_value") or 0)
            if analysis_ev != 0 and (item.get("analysis") or {}).get("consensus_reached"):
                mkt_ev = analysis_ev
            decision = await user_engine.evaluate_bet(
                match_info={
                    "id": int(item["match_id"]),
                    "odds": odds_map,
                    "provider_code": str(rec.get("provider_code") or ""),
                    "home_team": item.get("home_team"),
                    "away_team": item.get("away_team"),
                    "league": item.get("league"),
                },
                analysis={
                    **(item.get("analysis") or {}),
                    "prediction": sel,
                    "bet_type": bt,
                    "confidence": conf,
                    "expected_value": mkt_ev,
                    "odds": float(rec.get("odds") or 0),
                    "provider_code": str(rec.get("provider_code") or ""),
                    "line": rec.get("line"),
                    "kelly_fraction": float((item.get("analysis") or {}).get("kelly_fraction") or 0),
                    "ev_passed": True,
                    "consensus_reached": bool(
                        (item.get("analysis") or {}).get("consensus_reached")
                    ),
                    "reasoning": str(rec.get("reasoning") or ""),
                },
                user_balance=spendable,
                daily_loss=daily_loss_amt,
                active_bets_count=active_count + len([d for d in decisions if d.should_bet]),
            )
            if decision.should_bet:
                logger.info(
                    "✅ 策略通过 | match=%s %s vs %s | %s/%s | conf=%.2f odds=%.2f EV=%.4f kelly=%.2f stake=$%s",
                    item.get("match_id"), item.get("home_team", "?"), item.get("away_team", "?"),
                    decision.bet_type, decision.selection,
                    float(decision.confidence or 0), float(decision.odds or 0),
                    float(decision.expected_value or 0), float(decision.kelly_fraction or 0),
                    decision.suggested_stake,
                )
                from app.ai.strategy_gates import stake_bounds

                _lo, max_amt = stake_bounds(strat_cfg)
                stake = Decimal(str(decision.suggested_stake or 0))
                if stake < _lo:
                    stake = max_amt
                stake = min(stake, max_amt, spendable)
                if stake < _lo or stake > max_amt:
                    decision = decision.model_copy(
                        update={
                            "should_bet": False,
                            "suggested_stake": Decimal("0"),
                            "reasoning": f"[不投注] 仓位须在 [{_lo:g},{max_amt:g}]（策略单笔上限）",
                        }
                    )
                else:
                    decision = decision.model_copy(update={"suggested_stake": stake})
            if not decision.should_bet:
                # 拒绝日志已在 strategy._reject 中输出，此处仅补充比赛名称
                logger.debug(
                    "  └─ match=%s %s vs %s",
                    item.get("match_id"), item.get("home_team", "?"), item.get("away_team", "?"),
                )
            decisions.append(decision)
            if item.get("recommendation") is not None:
                item["recommendation"] = {
                    **(item.get("recommendation") or {}),
                    "should_bet": bool(decision.should_bet),
                    "confidence": float(decision.confidence or conf),
                    "win_rate": round(float(decision.confidence or conf) * 100, 1),
                    "expected_value": float(decision.expected_value or mkt_ev),
                    "suggested_stake": float(decision.suggested_stake or 0),
                    "reasoning": decision.reasoning,
                    "selection": decision.selection,
                    "odds": float(decision.odds or rec.get("odds") or 0),
                    "provider_code": decision.provider_code or rec.get("provider_code"),
                    "line": decision.line if decision.line is not None else rec.get("line"),
                }

        await self._publish_analyses_to_recs_cache(analyses)

        approved = [d for d in decisions if d.should_bet]
        # 构建分析摘要供前端日志展示
        analysis_summary = []
        for item in analyses:
            rec = item.get("recommendation") or {}
            ana = item.get("analysis") or {}
            conf = float(rec.get("confidence") or ana.get("confidence") or 0)
            if conf > 1:
                conf /= 100.0
            analysis_summary.append({
                "home_team": item.get("home_team", "?"),
                "away_team": item.get("away_team", "?"),
                "bet_type": rec.get("bet_type") or ana.get("bet_type") or "total",
                "selection": rec.get("selection") or "",
                "confidence": round(conf * 100, 1),
                "odds": float(rec.get("odds") or 0),
                "should_bet": bool(rec.get("should_bet")),
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

        approved.sort(key=_decision_profit_score, reverse=True)
        max_per_cycle = max(1, int(getattr(settings, "AI_MAX_BETS_PER_CYCLE", 3) or 3))
        cycle_cap = min(remaining_daily, max_per_cycle)
        seen_matches: set[int] = set()
        top_bets: list[BetDecision] = []
        for d in approved:
            mid = int(d.match_id or 0)
            if mid <= 0 or mid in seen_matches:
                continue
            conf = float(d.confidence or 0)
            if conf > 1:
                conf /= 100.0
            od = float(d.odds or 0)
            edge = round(conf * od - 1.0, 4) if od > 1 else float(d.expected_value or 0)
            d = d.model_copy(update={"expected_value": edge})
            seen_matches.add(mid)
            top_bets.append(d)
            if len(top_bets) >= cycle_cap:
                break
        for i, d in enumerate(top_bets, 1):
            sc = _decision_profit_score(d)
            logger.info(
                "本轮优选#%s match=%s sel=%s odds=%.3f wr=%.1f%% EV=%.4f exp_profit=%.2f",
                i,
                d.match_id,
                d.selection,
                float(d.odds or 0),
                float(d.confidence or 0) * (100 if float(d.confidence or 0) <= 1 else 1),
                float(d.expected_value or 0),
                sc[0],
            )
        logger.info(
            "本轮候选可投 %s → 利益最大化取 %s 场不同比赛（上限 %s，日剩余 %s）",
            len(approved),
            len(top_bets),
            max_per_cycle,
            remaining_daily,
        )

        # --- Phase 3: 新会话执行下单 ---
        executed = 0
        placed_matches: set[int] = set()
        async with AsyncSessionLocal() as db:
            user = await db.get(User, self.user_id)
            if not user or not user.ai_enabled:
                self.is_running = False
                return
            # 预加载策略/风控/今日注单数，避免循环内重复查询
            ai_config, strat_cfg = await load_fresh_strategy(self.user_id)
            today_count = 0
            should_stop = True
            reason = ""
            if not ai_config:
                strat_cfg = None
            else:
                strategy_engine.config = strat_cfg
                today_count = await self._count_today_bets(db, user)
                should_stop, reason = await self._check_risk(db, user, ai_config)
                if should_stop:
                    logger.info("执行中触发风控，停止本轮: %s", reason)
            for decision in top_bets:
                if executed >= cycle_cap:
                    break
                mid = int(decision.match_id or 0)
                if mid in placed_matches:
                    logger.info("跳过同场重复: match=%s", mid)
                    continue
                # 跨轮次防重复：跳过今天已下注的比赛
                if mid > 0 and await self._match_already_bet(db, self.user_id, mid):
                    logger.info("跳过今日已下注比赛: match=%s", mid)
                    continue
                if not ai_config or should_stop:
                    break
                if today_count >= int(strat_cfg.max_daily_bets):
                    logger.info("已达每日投注上限，停止本轮")
                    break
                capped = min(
                    Decimal(str(decision.suggested_stake or 0)),
                    Decimal(str(strat_cfg.max_bet_amount)),
                )
                decision = decision.model_copy(update={"suggested_stake": capped})
                ok_pass, why = decision_passes_strategy(decision, strat_cfg)
                if not ok_pass:
                    logger.info("最新策略拦截 match=%s: %s", decision.match_id, why)
                    continue
                analysis_payload = {}
                for item in analyses:
                    if int(item.get("match_id") or 0) == decision.match_id:
                        analysis_payload = item.get("analysis") or {}
                        break
                if not auto_place:
                    await self._notify(user.id, "manual_recommend", {
                        **decision.model_dump(),
                        "bet_mode": bet_mode,
                        "strategy": strat_cfg.model_dump(),
                        "message": "人工模式：已按最新 AI 策略生成推荐，请手动确认后真实下单",
                    })
                    continue
                ok = await self._execute_bet(
                    db,
                    user,
                    decision,
                    analysis_cache=analysis_payload,
                    ai_config=ai_config,
                )
                if ok:
                    executed += 1
                    active_count += 1
                    if mid > 0:
                        placed_matches.add(mid)
                    await db.commit()
                    # 下注成功后余额变化，刷新策略
                    ai_config, strat_cfg = await load_fresh_strategy(self.user_id)
                    if ai_config:
                        strategy_engine.config = strat_cfg
                    today_count += 1
            await db.commit()

        await self._notify(self.user_id, "cycle_complete", {
            "analyzed": len(analyses),
            "candidates": len(candidates),
            "approved": len(approved),
            "executed": executed,
            "max_per_cycle": max_per_cycle,
            "cycle_cap": cycle_cap,
            "placed_matches": sorted(placed_matches),
            "bet_mode": bet_mode,
            "auto_place": auto_place,
            "market": "moneyline,spread,total",
            "scope": "live",
            "analysis_summary": analysis_summary,
            "decisions": [d.model_dump() for d in top_bets],
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
        from app.ai.strategy import effective_strategy_from_ai_config

        from app.ai.strategy_gates import team_is_excluded

        scan_strat = effective_strategy_from_ai_config(ai_config)
        min_od = float(scan_strat.min_odds)
        max_od = float(scan_strat.max_odds)
        excluded = list(getattr(ai_config, "excluded_teams", None) or [])

        # 刚开赛优先扫描
        matches = sort_just_started_first(matches)
        candidates = []
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
            why = skip_reason_for_match(m, None, min_odds=min_od, max_odds=max_od)
            if why:
                logger.debug("scan skip match=%s reason=%s", m.id, why)
                continue
            from app.ai.market_recommend import load_all_market_odds_pack

            odds_pack = await load_all_market_odds_pack(
                db, m.id, sport_key, providers_filter=SINGLE_SIDE_PROVIDER_NAMES
            )
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
                min_odds=min_od,
                max_odds=max_od,
            )
            if why:
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
        strategy_engine.config = strat_cfg

        # 策略硬门槛：置信度 / 赔率 / 日限额 / 仓位
        if float(decision.confidence or 0) + 1e-9 < float(strat_cfg.min_confidence):
            logger.warning(
                "⚠️ 下单拒绝 | match=%s | 门禁=置信度不足 | conf=%.2f < 策略%.2f",
                decision.match_id, float(decision.confidence or 0), strat_cfg.min_confidence,
            )
            return False
        odds_v = float(decision.odds or 0)
        if odds_v + 1e-9 < float(strat_cfg.min_odds) or odds_v - 1e-9 > float(strat_cfg.max_odds):
            logger.warning(
                "⚠️ 下单拒绝 | match=%s | 门禁=赔率越界 | odds=%.2f 不在[%.1f, %.1f]",
                decision.match_id, odds_v, float(strat_cfg.min_odds), float(strat_cfg.max_odds),
            )
            return False
        today_count = await self._count_today_bets(db, user)
        if today_count >= int(strat_cfg.max_daily_bets):
            logger.warning(
                "⚠️ 下单拒绝 | match=%s | 门禁=每日上限 | 今日已下%d/%s",
                decision.match_id, today_count, strat_cfg.max_daily_bets,
            )
            return False

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
        allowed = {
            "total": {"over", "under"},
            "moneyline": {"home", "away", "draw"},
            "spread": {"home", "away"},
        }
        if bet_type not in allowed or sel not in allowed[bet_type]:
            logger.warning(
                "AI 不支持的盘口/方向: match=%s type=%s sel=%s",
                decision.match_id, bet_type, sel,
            )
            return False

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

        bt_enum = {
            "total": BetType.TOTAL,
            "moneyline": BetType.MONEYLINE,
            "spread": BetType.SPREAD,
        }[bet_type]
        odds_row = await self._get_odds_row(
            db,
            decision.match_id,
            provider_name_prefer=provider_label,
            bet_type=bt_enum,
        )
        odds_payload = dict(odds_row.odds_data or {}) if odds_row else {}
        line_val = None
        if bet_type == "total":
            if odds_row and odds_row.total is not None:
                try:
                    line_val = float(odds_row.total)
                except (TypeError, ValueError):
                    line_val = None
        elif bet_type == "spread":
            if odds_row and odds_row.spread is not None:
                try:
                    line_val = float(odds_row.spread)
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
            elif bet_type == "spread":
                odds_payload = {**odds_payload, "line": line_val, "spread": line_val}

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

        # 赔率变动检测：最新赔率与决策赔率不一致时，检查是否低于最低赔率
        decision_odds = float(decision.odds or 0)
        if decision_odds > 0 and abs(current_odds - decision_odds) > 0.05:
            # 赔率逆向变动（上升）：市场不认可我们的方向，放弃下注
            if current_odds > decision_odds + 0.05:
                logger.warning(
                    "⚠️ 下单拒绝 | match=%s | 门禁=赔率逆向变动 | 决策=%.2f 最新=%.2f 变动=+%.1f%% | 市场不认可",
                    decision.match_id, decision_odds, current_odds,
                    (current_odds - decision_odds) / decision_odds * 100,
                )
                await self._notify(user.id, "bet_failed", {
                    "match_id": decision.match_id,
                    "message": f"赔率逆向变动 {decision_odds:.2f}→{current_odds:.2f}，市场不认可，放弃下注",
                })
                return False
            if current_odds + 1e-9 < float(strat_cfg.min_odds):
                logger.warning(
                    "⚠️ 下单拒绝 | match=%s | 门禁=赔率低于下限 | 决策=%.2f 最新=%.2f min=%.1f",
                    decision.match_id, decision_odds, current_odds, float(strat_cfg.min_odds),
                )
                await self._notify(user.id, "bet_failed", {
                    "match_id": decision.match_id,
                    "message": f"赔率降至 {current_odds:.2f}，低于最低赔率 {strat_cfg.min_odds}",
                })
                return False
            logger.info(
                "📋 赔率变动可接受 | match=%s | 决策=%.2f 最新=%.2f (使用最新赔率)",
                decision.match_id, decision_odds, current_odds,
            )

        # 异常高赔率校验：避免数据错误导致异常下单
        if current_odds > 3.5:
            logger.warning(
                "⚠️ 下单拒绝 | match=%s | 门禁=异常高赔率 | type=%s sel=%s odds=%.2f > 3.5",
                decision.match_id, bet_type, sel, current_odds,
            )
            await self._notify(user.id, "bet_failed", {
                "match_id": decision.match_id,
                "message": f"赔率 {current_odds:.2f} 异常过高，跳过下单",
            })
            return False

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

        from app.ai.strategy_gates import stake_bounds

        _lo, max_amt = stake_bounds(strat_cfg)
        max_amt = max_amt.quantize(Decimal("0.01"))
        stake = min(Decimal(str(decision.suggested_stake)).quantize(Decimal("0.01")), max_amt)
        if stake < _lo:
            # 默认用策略单笔上限
            stake = max_amt
        if stake < _lo or stake > max_amt:
            logger.info("拒绝下单：仓位 %s 不在策略区间 [%s,%s]", stake, _lo, max_amt)
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
                        elif bet_type == "spread":
                            fresh_payload = {**fresh_payload, "line": line_val, "spread": line_val}
                    odds_payload = fresh_payload
                    try:
                        current_odds = float(
                            fresh_payload.get(sel)
                            or fresh_payload.get("odds")
                            or current_odds
                        )
                    except (TypeError, ValueError):
                        pass
                # 赔率低于最低赔率 -> 放弃补单
                if current_odds + 1e-9 < float(strat_cfg.min_odds):
                    logger.warning(
                        "补单放弃：赔率 %.3f < 最低赔率 %.3f match=%s",
                        current_odds, float(strat_cfg.min_odds), decision.match_id,
                    )
                    await self._notify(user.id, "bet_failed", {
                        "match_id": decision.match_id,
                        "message": f"赔率降至 {current_odds:.2f}，低于最低赔率 {strat_cfg.min_odds}，放弃补单",
                    })
                    if _resume_fn:
                        _resume_fn()
                    return False
                logger.info(
                    "补单重试 %s/%s: match=%s 最新赔率=%.3f",
                    attempt, retry_count, decision.match_id, current_odds,
                )

            place = await connector.place_bet(
                match_external_id=str(match_ext or match.external_id),
                selection=sel,
                odds=float(current_odds),
                stake=stake,
                bet_type=bet_type,
                odds_data=odds_payload,
            )
            if place.ok:
                # OB 站点：下单后验证 orderNo 是否真实存在
                if provider_code == "ob" and place.external_bet_id:
                    # 先记录 orderNo 到 Redis（无论验证结果），防止下轮重复下单
                    await self._mark_bet_pending(decision.match_id, place.external_bet_id)

                    verified = await self._verify_ob_bet(
                        site_acc, place.external_bet_id
                    )
                    if not verified:
                        logger.warning(
                            "OB 下单验证失败: orderNo=%s 在 OB 注单列表中不存在，视为未下单",
                            place.external_bet_id,
                        )
                        place.ok = False
                        place.message = f"OB 返回 orderNo={place.external_bet_id} 但验证不存在"
                        # OB 已返回 orderNo，可能实际已下单；不再重试避免重复下单
                        logger.warning("OB 已返回 orderNo，不再重试避免重复下单")
                        break
                    else:
                        logger.info(
                            "OB 下单验证通过: orderNo=%s",
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

        type_label = {"total": "大小", "moneyline": "胜负", "spread": "让球"}.get(bet_type, bet_type)
        tx = Transaction(
            user_id=user.id,
            type=TransactionType.AI_BET,
            amount=Decimal("0"),
            balance_after=user.balance,
            bet_id=bet.id,
            description=(
                f"AI滚球{type_label}: {match.home_team} vs {match.away_team} "
                f"[{sel}{line_tag} @ {current_odds}] EV={decision.expected_value}"
            ),
        )
        db.add(tx)

        logger.info(
            "AI真实投注成功: match=%s type=%s sel=%s line=%s stake=%s odds=%s conf=%.2f ext=%s",
            decision.match_id, bet_type, sel, line_val, stake, current_odds,
            decision.confidence, place.external_bet_id,
        )

        await self._notify(user.id, "ai_bet_placed", {
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
        # 验证通过且已入库，清除 Redis 待定标记
        try:
            from app.core.cache import cache
            await cache.delete(f"ai:bet:pending:{self.user_id}:{decision.match_id}")
        except Exception:
            pass
        if _resume_fn:
            _resume_fn()
        return True

    async def _verify_ob_bet(self, site_acc, order_no: str) -> bool:
        """下单后验证 OB 注单是否真实存在（防止 OB API 假成功）。"""
        import asyncio as _aio
        import httpx
        from app.services.bookmakers.gate_client import _gate_headers
        from app.core.crypto import decrypt_secret

        gate = (settings.BOOKMAKER_BROWSER_GATE_URL or "").rstrip("/")
        if not gate:
            return True  # 无 gate 则跳过验证

        await _aio.sleep(2.0)  # 等 OB 后端写入注单
        try:
            async with httpx.AsyncClient(timeout=45.0, headers=_gate_headers()) as client:
                resp = await client.post(
                    f"{gate}/bets/history",
                    json={
                        "site_code": "ob",
                        "base_url": site_acc.base_url or "",
                        "session_token": decrypt_secret(site_acc.session_token_encrypted),
                        "days": 1,
                    },
                )
                data = resp.json() if resp.status_code < 500 else {}
                orders = data.get("orders") or []
                for od in orders:
                    if str(od.get("external_bet_id") or "") == str(order_no):
                        return True
                return False
        except Exception as e:
            logger.warning("OB 下单验证异常: %s", e)
            return False  # 验证异常时拒绝，避免误判为下单成功

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
        """优先取指定站盘口行（默认全场大小球）。"""
        result = await db.execute(
            select(Odds).where(
                Odds.match_id == match_id,
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
                Odds.match_id == match_id,
                Odds.bet_type == bet_type,
                Odds.valid_to.is_(None),
            )
        )
        odds_list = result.scalars().all()
        if odds_list:
            return odds_list[0]
        # 兜底：任意有效行
        result = await db.execute(
            select(Odds).where(
                Odds.match_id == match_id,
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
        """统计活跃投注数"""
        result = await db.execute(
            select(func.count(Bet.id)).where(
                Bet.user_id == user.id,
                Bet.status == BetStatus.SUCCESS
            )
        )
        return result.scalar_one()

    async def _count_today_bets(self, db: AsyncSession, user: User) -> int:
        """今日已产生的 AI 投注笔数（含已结算）。"""
        today = datetime.now(timezone.utc).date()
        today_start = datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc)
        result = await db.execute(
            select(func.count(Bet.id)).where(
                Bet.user_id == user.id,
                Bet.is_ai_bet.is_(True),
                Bet.created_at >= today_start,
            )
        )
        return int(result.scalar_one() or 0)

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

    async def _mark_bet_pending(self, match_id: int, order_no: str) -> None:
        """记录 OB 返回了 orderNo 的待定注单（TTL 6 小时，防止跨轮次重复下单）。"""
        try:
            from app.core.cache import cache
            # 记录 match_id + orderNo，TTL 6h（覆盖一个比赛日）
            await cache.set_json(
                f"ai:bet:pending:{self.user_id}:{match_id}",
                {"order_no": order_no, "time": datetime.now(timezone.utc).isoformat()},
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
        user_engine = StrategyEngine(strat_cfg)
        strategy_engine.config = strat_cfg

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
        try:
            ed = match_info["extra_data"]
        except Exception:
            pass
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
            min_odds=float(strat_cfg.min_odds),
            max_odds=float(strat_cfg.max_odds),
        )
        if skip_why:
            await db.commit()
            reason_map = {
                "score_over_line": "当前比分已超过大小球盘口，跳过分析",
                "ending_soon": "比赛预计 10 分钟内结束，跳过分析",
                "china_match": "中国赛事已过滤，跳过分析",
                "odds_out_of_range": (
                    f"赔率不在配置区间 [{strat_cfg.min_odds:g},{strat_cfg.max_odds:g}]，跳过分析"
                ),
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
    from app.ai.match_context import fetch_match_context

    use_llm = bool(strat_cfg.use_llm_analysis)
    if shared_analysis is not None:
        analysis = dict(shared_analysis)
        ctx = dict(shared_ctx or {})
        news_list = list(ctx.get("news_injuries") or [])
        match_info["recent_form"] = {
            "home": ctx.get("home_form") or {},
            "away": ctx.get("away_form") or {},
        }
        match_info["context_source"] = ctx.get("source") or "shared"
        llm_down = bool(analysis.get("error")) or not analysis.get("models_used")
        conf0 = float(analysis.get("confidence") or 0.5)
    elif not use_llm:
        ctx = {}
        news_list = []
        match_info["recent_form"] = {"home": {}, "away": {}}
        match_info["context_source"] = "strategy_market"
        llm_down = True
        conf0 = 0.0
        analysis = {
            "prediction": "over",
            "confidence": 0.0,
            "reasoning": "[策略] 未启用 LLM，按盘口 EV + AI 策略阈值决策",
            "models_used": [],
            "consensus_reached": True,
            "ev_passed": True,
            "error": None,
        }
    else:
        ctx: dict = {}
        # 批量推荐传 skip_match_context=True，跳过赛前上下文 LLM
        if skip_match_context:
            ctx = {}
        else:
            try:
                ctx = await asyncio.wait_for(fetch_match_context(match_info), timeout=30.0)
            except asyncio.TimeoutError:
                logger.warning("match_context fetch timeout, skip")
                ctx = {}
            except Exception as e:
                logger.warning("fetch_match_context failed match=%s: %s", match_id, e)
                ctx = {}

        historical_pack = {
            "h2h": ctx.get("h2h") or {},
            "home_form": ctx.get("home_form") or {},
            "away_form": ctx.get("away_form") or {},
            "news_injuries": ctx.get("news_injuries") or [],
            "player_status": ctx.get("player_status") or ctx.get("news_injuries") or [],
            "player_stats": ctx.get("player_stats") or {},
            "motivation": ctx.get("motivation") or {},
            "lineup": ctx.get("lineup") or {},
            "standings": ctx.get("standings") or {},
            "weather": ctx.get("weather") or {},
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
        news_list = list(ctx.get("news_injuries") or [])
        if ctx.get("player_status"):
            news_list = news_list + [f"状态:{x}" for x in (ctx.get("player_status") or [])[:4]]

        analysis = await analyzer.analyze_match(
            match_info,
            historical_data=historical_pack,
            market_odds={
                "markets": markets_odds,
                "line_movements": odds_pack.get("line_movements") or {},
                "odds_style": "asian",
                **odds,
            },
            news=news_list,
        )
        llm_down = bool(analysis.get("error")) or not analysis.get("models_used")
        conf0 = float(analysis.get("confidence") or 0.5)
        ctx_bits = []
        h2n = len((ctx.get("h2h") or {}).get("matches") or [])
        hf = len((ctx.get("home_form") or {}).get("matches") or [])
        af = len((ctx.get("away_form") or {}).get("matches") or [])
        nn = len(ctx.get("news_injuries") or [])
        src = str(ctx.get("source") or "none")
        if h2n or hf or af or nn:
            ctx_bits.append(f"交锋{h2n}/主近况{hf}/客近况{af}/伤病{nn}(源:{src})")
            if news_list:
                ctx_bits.append("伤病要点:" + ";".join(news_list[:2]))
        else:
            ctx_bits.append(f"交锋/近10场/伤病:暂无数据(源:{src})")
        ctx_note = "[" + " | ".join(ctx_bits) + "] "

        failed = analysis.get("models_failed") or []
        fail_note = ""
        if failed:
            fail_note = f"[失败模型: {', '.join(str(x) for x in failed[:6])}] "
        err = str(analysis.get("error") or "")
        quota_hit = any(
            x in (err + fail_note + str(analysis.get("reasoning") or "")).lower()
            for x in ("401008", "quota", "额度", "402", "rate limit", "429", "exhausted")
        )

        if llm_down:
            # 真正无可用模型 / 超时：可展示盘口启发式，但禁止放行共识/EV 门禁
            why = "LLM 暂不可用"
            if "timeout" in err.lower() or err == "ensemble_timeout":
                why = "LLM 分析超时"
            elif quota_hit:
                why = "部分模型额度耗尽/未开通后付费"
            analysis = {
                **analysis,
                "confidence": min(float(conf0 or 0), 0.49),
                "consensus_reached": False,
                "ev_passed": False,
                "should_bet": False,
                "reasoning": (
                    f"[盘口启发式·不可下单] {why}，共识/EV 未通过，仅供参考。 "
                    + fail_note
                    + ctx_note
                    + str(analysis.get("reasoning") or "")
                )[:500],
            }
        elif analysis.get("consensus_reached") is False:
            # 有模型返回但共识不足：不得伪造 consensus/ev_passed
            analysis = {
                **analysis,
                "ev_passed": False,
                "should_bet": False,
                "reasoning": (
                    "[共识不足·不可下单] 模型未达共识门槛，禁止放行。 "
                    + fail_note
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
        pref_bt = str(analysis.get("bet_type") or "total").lower().strip()
        if pref_bt not in ("total", "moneyline", "spread"):
            pref_bt = "total"
        pred = str(analysis.get("prediction") or "").lower().strip()
        allowed_sels = {
            "total": {"over", "under"},
            "moneyline": {"home", "away", "draw"},
            "spread": {"home", "away"},
        }
        if pred not in allowed_sels.get(pref_bt, ()):
            pred = ""

        markets = await build_match_market_recommendations(
            db,
            match_id=match_id,
            sport=sport_key,
            prediction=pred,
            confidence=conf_use if conf_use > 0 else conf0,
            stake=default_stake if default_stake >= settings.AI_MIN_BALANCE else max_amt,
            providers_filter=providers_filter,
            min_odds=float(strat_cfg.min_odds),
            max_odds=float(strat_cfg.max_odds),
            preferred_bet_type=pref_bt,
        )
        primary_market = next(
            (m for m in markets if str(m.get("bet_type") or "") == pref_bt and m.get("single")),
            None,
        )
        if not primary_market:
            primary_market = next((m for m in markets if m.get("single")), None)
        primary = dict((primary_market or {}).get("single") or {})
        bet_type = str(
            (primary_market or {}).get("bet_type")
            or primary.get("bet_type")
            or pref_bt
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

        sel = str(primary.get("selection") or pred or "").lower()
        sel_label = primary.get("selection_label") or SEL_LABELS.get(sel, sel)
        sel_odds = float(primary.get("odds") or market_odds_flat.get(sel) or 0)
        try:
            wr = float(primary.get("win_rate"))
            if wr > 1:
                conf_use = max(0.01, min(0.99, wr / 100.0))
            elif wr > 0:
                conf_use = max(0.01, min(0.99, wr))
        except (TypeError, ValueError):
            pass
        if sel in allowed_sels.get(bet_type, ()):
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

        if not primary or sel not in allowed_sels.get(bet_type, ()) or sel_odds <= 1:
            decision = BetDecision(
                match_id=match_id,
                selection=sel or "over",
                confidence=conf_use,
                expected_value=0,
                kelly_fraction=0,
                suggested_stake=Decimal("0"),
                reasoning="[不投注] 无可用盘口",
                risk_score=1.0,
                should_bet=False,
                bet_type=bet_type,
                provider_code=provider_code,
                odds=sel_odds,
                line=float(line) if line is not None else None,
            )
        else:
            # EV 计算：优先用 analyze_match 返回的去 vig EV，否则用原始赔率计算
            from app.ai.analyzer import _devig_ev
            analysis_ev = float(analysis.get("expected_value") or 0)
            if analysis_ev != 0 and analysis.get("consensus_reached"):
                mkt_ev = round(analysis_ev, 4)
            elif sel_odds > 1 and len(market_odds_flat) >= 2:
                mkt_ev = _devig_ev(conf_use, sel_odds, market_odds_flat)
            else:
                mkt_ev = round(conf_use * sel_odds - 1.0, 4) if sel_odds > 1 else 0.0
            primary["expected_value"] = mkt_ev
            primary["win_rate"] = round(conf_use * 100, 1)
            consensus_ok = True if not use_llm else bool(analysis.get("consensus_reached", True))
            decision = await user_engine.evaluate_bet(
                match_info={
                    **match_info,
                    "odds": market_odds_flat,
                    "provider_code": provider_code,
                    "bet_type": bet_type,
                },
                analysis={
                    **analysis,
                    "prediction": sel,
                    "bet_type": bet_type,
                    "confidence": conf_use,
                    "expected_value": mkt_ev,
                    "odds": sel_odds,
                    "provider_code": provider_code,
                    "line": line,
                    "ev_passed": True,
                    "consensus_reached": consensus_ok,
                },
                user_balance=balance,
                daily_loss=daily_loss_amt,
                active_bets_count=active_count,
            )

        from app.ai.strategy_gates import check_daily_risk, stake_bounds

        _lo, _hi = stake_bounds(strat_cfg)
        # 仓位严格=策略单笔最大金额（调用方显式传入 stake 时再覆盖）
        sug_stake = _hi
        if stake is not None and Decimal(str(stake)) > 0:
            sug_stake = min(Decimal(str(stake)), _hi)
        if decision.should_bet and (sug_stake < _lo or sug_stake > _hi):
            decision = decision.model_copy(
                update={
                    "should_bet": False,
                    "reasoning": f"[不投注] 仓位须在 [{_lo:g},{_hi:g}]（策略单笔上限）",
                }
            )
            sug_stake = Decimal("0")
        elif decision.should_bet:
            decision = decision.model_copy(update={"suggested_stake": sug_stake})
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

        context_meta = {
            "source": ctx.get("source") or "none",
            "model_used": ctx.get("model_used"),
            "fetched_at": ctx.get("fetched_at"),
            "cache_hit": bool(ctx.get("cache_hit")),
            "h2h_count": len((ctx.get("h2h") or {}).get("matches") or []),
            "home_form_count": len((ctx.get("home_form") or {}).get("matches") or []),
            "away_form_count": len((ctx.get("away_form") or {}).get("matches") or []),
            "news_count": len(ctx.get("news_injuries") or []),
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
                "should_bet": bool(decision.should_bet and primary),
                "selection": sel,
                "selection_label": sel_label,
                "confidence": decision.confidence,
                "win_rate": win_rate,
                "suggested_stake": float(sug_stake),
                "reasoning": decision.reasoning,
                "expected_value": primary.get("expected_value", decision.expected_value),
                "risk_score": decision.risk_score,
                "provider": primary.get("provider") or provider_name_str,
                "provider_code": primary.get("provider_code") or provider_code,
                "odds": sel_odds or decision.odds,
                "bet_type": bet_type,
                "line": line,
                "llm_fallback": bool(llm_down),
            },
            "strategy": {
                "name": strat_cfg.name,
                "min_confidence": strat_cfg.min_confidence,
                "min_odds": strat_cfg.min_odds,
                "max_odds": strat_cfg.max_odds,
                "max_bet_amount": strat_cfg.max_bet_amount,
                "max_daily_bets": strat_cfg.max_daily_bets,
                "stop_loss": strat_cfg.stop_loss,
                "take_profit": strat_cfg.take_profit,
                "use_llm_analysis": strat_cfg.use_llm_analysis,
            },
            "current_odds": {
                k: v for k, v in (market_odds_flat or {}).items()
                if k in ("over", "under", "home", "away", "draw") and isinstance(v, (int, float))
            },
            "best_by_selection": mkt_pack.get("best_by_selection") or {},
            "odds_by_provider": mkt_pack.get("odds_by_provider") or {},
            "site_scope": "ob,pinnacle",
            "market_scope": "moneyline,spread,total" if str(sport_key).lower() in ("football", "soccer") else "total",
            "match_status": match_status,
        }


async def analyze_fixture_group(
    match_ids: list[int],
    user_id: int,
    *,
    stake: float | None = None,
    skip_match_context: bool | None = None,
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
    # Load strategy once for the entire fixture group (avoid redundant load_fresh_strategy per match)
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
        "news_injuries": [],
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
    """同场多站推荐中择优一单（优先可投 + EV）。"""
    if not recs:
        return None
    return max(
        recs,
        key=lambda r: (
            1 if (r.get("recommendation") or {}).get("should_bet") else 0,
            float((r.get("recommendation") or {}).get("expected_value") or 0),
            float((r.get("recommendation") or {}).get("confidence") or 0),
        ),
    )


# === 全局引擎管理 ===
_active_engines: dict[int, AIBettingEngine] = {}


async def start_user_engine(user_id: int) -> dict:
    """为用户启动AI引擎。一律真实下单，并由 bet_mode 控制是否自动执行。"""
    if user_id in _active_engines:
        await _active_engines[user_id].stop()

    engine = AIBettingEngine(user_id)
    _active_engines[user_id] = engine
    await engine.start()

    # 写入 Redis 供跨 worker 查询
    try:
        from app.core.cache import cache
        await cache.set(f"ai:engine:running:{user_id}", "1", ttl=0)
    except Exception:
        pass

    return {"status": "started", "user_id": user_id, "bet_mode_gated": True}


async def stop_user_engine(user_id: int) -> dict:
    """停止用户AI引擎"""
    if user_id in _active_engines:
        await _active_engines[user_id].stop()
        del _active_engines[user_id]
    # 清除 Redis 标记
    try:
        from app.core.cache import cache
        await cache.delete(f"ai:engine:running:{user_id}")
    except Exception:
        pass
    return {"status": "stopped", "user_id": user_id}


async def get_engine_status(user_id: int) -> dict:
    """获取引擎状态（跨 worker：内存优先，Redis 兜底）"""
    engine = _active_engines.get(user_id)
    if engine and engine.is_running:
        return {"running": True}
    # 当前 worker 无引擎，查 Redis 看是否在其他 worker 运行
    try:
        from app.core.cache import cache
        val = await cache.get(f"ai:engine:running:{user_id}")
        if val and str(val) in ("1", "true", "True"):
            return {"running": True}
    except Exception:
        pass
    return {"running": False}
