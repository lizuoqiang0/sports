"""
投注 API - 下单 / 撤单 / 兑现 / 历史
"""
import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

from app.database import get_db, AsyncSessionLocal
from app.models.user import (
    User, Match, Odds, Bet, BetStatus, BetType, Transaction, TransactionType,
    MatchStatus, BookmakerAccount, BookmakerStatus,
)
from app.core.security import get_current_user, check_rate_limit
from app.core.websocket import manager, WSEventType
from app.schemas import (
    APIResponse, PlaceBetRequest, PlaceBetResponse,
)
from app.config import settings, today_start_utc, month_start_utc

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/bets", tags=["投注"])
LOCAL_RECORDED_BET_STATUSES = frozenset({BetStatus.SUCCESS})
PENDING_RECONCILE_TIMEOUT_SEC = 4.0
PENDING_LIST_TIMEOUT_SEC = 2.0
_BET_LOCK_TTL_SEC = 180
_BET_LOCK_RENEW_SEC = 30


async def _renew_bet_lock(key: str, token: str) -> None:
    """在真实站点响应缓慢时保持下单互斥锁，避免 TTL 到期后重复提交。"""
    from app.core.cache import cache

    try:
        while True:
            await asyncio.sleep(_BET_LOCK_RENEW_SEC)
            if not await cache.extend_lock_if_owned(
                key, token, ttl_sec=_BET_LOCK_TTL_SEC
            ):
                logger.error("下单锁续期失败 key=%s；等待当前请求结束", key)
                return
    except asyncio.CancelledError:
        raise


async def _load_pending_items_for_view(user_id: int, *, scene: str) -> list[dict]:
    try:
        from app.services.bookmakers.pending_bets import reconcile_pending_ai_bets

        await asyncio.wait_for(
            reconcile_pending_ai_bets(user_id),
            timeout=PENDING_RECONCILE_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "%s补录待定单超时 user=%s timeout=%.1fs，先返回已确认数据",
            scene,
            user_id,
            PENDING_RECONCILE_TIMEOUT_SEC,
        )
    except Exception as e:
        logger.warning("%s补录待定单失败 user=%s: %s", scene, user_id, e)

    try:
        from app.services.bookmakers.pending_bets import list_pending_ai_bets

        return await asyncio.wait_for(
            list_pending_ai_bets(user_id),
            timeout=PENDING_LIST_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "%s读取待定单超时 user=%s timeout=%.1fs，按无待定返回",
            scene,
            user_id,
            PENDING_LIST_TIMEOUT_SEC,
        )
    except Exception as e:
        logger.warning("%s读取待定单失败 user=%s: %s", scene, user_id, e)
    return []


# === 下注 ===
@router.post("/place", response_model=APIResponse)
async def place_bet(
    req: PlaceBetRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    下注核心流程:
    1. 验证赛事存在且未开始/进行中
    2. 验证赔率未被篡改
    3. 锁定用户余额
    4. 创建投注记录
    5. 创建交易流水
    6. WebSocket推送通知
    """
    # 速率限制: 每分钟最多5次下单
    if not check_rate_limit(current_user.id, "place_bet", max_requests=5, window_seconds=60):
        raise HTTPException(status_code=429, detail="下单过于频繁，请稍候")

    # 1. 获取赛事
    match_result = await db.execute(
        select(Match).where(Match.id == req.match_id)
    )
    match = match_result.scalar_one_or_none()
    if not match:
        raise HTTPException(status_code=404, detail="赛事不存在")

    if match.status not in [MatchStatus.UPCOMING, MatchStatus.LIVE]:
        raise HTTPException(status_code=400, detail=f"赛事状态不允许投注: {match.status}")

    # 同场（OB/平博重复）只允许一单：任一侧已有未结算注单则拒绝
    from app.services.fixture_key import sibling_match_ids

    try:
        sib_ids = await sibling_match_ids(db, match)
    except Exception:
        sib_ids = [int(match.id)]
    existing_open = await db.execute(
        select(Bet.id).where(
            Bet.user_id == current_user.id,
            Bet.match_id.in_(sib_ids),
            Bet.status == BetStatus.SUCCESS,
            Bet.settled_at.is_(None),
        ).limit(1)
    )
    if existing_open.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=400,
            detail="该场比赛已有未结算注单（OB/平博同场只投一单）",
        )

    sel = str(req.selection or "").lower()
    bt = str(req.bet_type or "").lower()
    # 只投全场小球：OB/平博下单链路仅对该方向做过可靠性验证。
    if bt != "total":
        raise HTTPException(
            status_code=400,
            detail="仅支持全场小球（bet_type=total）",
        )
    if sel != "under":
        raise HTTPException(status_code=400, detail="仅支持小球选项（selection=under）")

    # 2. 解析目标站点并获取赔率
    from app.core.crypto import decrypt_secret
    from app.services.bookmakers.catalog import BOOKMAKER_CATALOG, provider_name
    from app.services.bookmakers.registry import get_connector
    from app.services.bookmakers.plugins.ob.kaiyun import is_demo_url
    from app.services.provider_utils import code_by_provider as _code_by_provider

    provider_raw = (req.provider or "").strip()
    provider_code = ""
    provider_label = ""
    if provider_raw:
        low = provider_raw.lower()
        if low in BOOKMAKER_CATALOG:
            provider_code = low
            provider_label = provider_name(low)
        else:
            provider_code = _code_by_provider(provider_raw) or ""
            provider_label = provider_raw
            if not provider_code:
                for code, meta in BOOKMAKER_CATALOG.items():
                    if meta["name"] == provider_raw:
                        provider_code = code
                        provider_label = meta["name"]
                        break

    # 查库用归一化后的小写值，避免客户端大小写不一致导致 PostgreSQL 查询落空。
    # 校验通过但 PostgreSQL 区分大小写查询落空
    from app.models.user import BetType as _BTEnum

    odds_query = select(Odds).where(
        Odds.match_id == req.match_id,
        Odds.bet_type == _BTEnum(bt),
        Odds.valid_to.is_(None),
    )
    if provider_label:
        odds_query = odds_query.where(Odds.provider == provider_label)
    odds_result = await db.execute(odds_query)
    odds_rows = list(odds_result.scalars().all())
    odds_obj = odds_rows[0] if odds_rows else None
    # 未指定站点或未命中：在多站中找匹配赔率的那一行
    if not odds_obj:
        all_res = await db.execute(
            select(Odds).where(
                Odds.match_id == req.match_id,
                Odds.bet_type == _BTEnum(bt),
                Odds.valid_to.is_(None),
            )
        )
        candidates = list(all_res.scalars().all())
        # 下单站点自己的行优先（其 line 才是结算依据）——
        # 跨站只比赔率不比线，两家 line 常差 0.25/0.5，接错行的线会错判输赢
        if provider_label:
            candidates.sort(key=lambda r: (r.provider or "") != provider_label)
        for row in candidates:
            if provider_label and (row.provider or "") != provider_label:
                # 有本站行时不再跨站接管（除非本站完全没有行）
                if any((r.provider or "") == provider_label for r in candidates):
                    continue
            try:
                v = float((row.odds_data or {}).get(sel) or 0)
            except (TypeError, ValueError):
                continue
            if abs(v - float(req.odds)) <= 0.05:
                odds_obj = row
                provider_label = row.provider or provider_label
                provider_code = provider_code or (_code_by_provider(provider_label) or "")
                break
        if not odds_obj and candidates:
            # 兜底：优先本站行，避免 bet.line 记录成另一站的线
            if provider_label:
                own = [r for r in candidates if (r.provider or "") == provider_label]
                odds_obj = own[0] if own else candidates[0]
            else:
                odds_obj = candidates[0]
            provider_label = odds_obj.provider or provider_label
            provider_code = provider_code or (_code_by_provider(provider_label) or "")

    if not odds_obj:
        raise HTTPException(status_code=404, detail="赔率不存在")

    raw_odds = (odds_obj.odds_data or {}).get(sel)
    if raw_odds is None or isinstance(raw_odds, (dict, list)):
        raise HTTPException(status_code=400, detail=f"无效选项: {req.selection}")
    current_odds = float(raw_odds)

    if not provider_label:
        provider_label = odds_obj.provider or "本地"
    if not provider_code:
        provider_code = _code_by_provider(provider_label) or (
            "ob" if str(match.external_id or "").startswith("ob:") else ""
        )

    if match.status == MatchStatus.LIVE:
        fresh_at = match.updated_at or odds_obj.valid_from
        if fresh_at is None:
            # 赔率版本可能来自历史数据但没有时间字段；先仅同步当前站点，
            # 再读取新盘口，不能带着缺失时间的行进入真实下单。
            try:
                refresh_acc_res = await db.execute(
                    select(BookmakerAccount).where(
                        BookmakerAccount.user_id == current_user.id,
                        BookmakerAccount.code == provider_code,
                        BookmakerAccount.enabled.is_(True),
                    )
                )
                refresh_acc = refresh_acc_res.scalar_one_or_none()
                if refresh_acc is not None:
                    from app.services.bookmakers.sync import sync_live_scores_odds

                    async with AsyncSessionLocal() as sync_db:
                        await sync_live_scores_odds(
                            sync_db,
                            user_id=current_user.id,
                            only_account_id=refresh_acc.id,
                            refresh_balance=False,
                        )
                    await db.rollback()
                    match = await db.get(Match, req.match_id)
                    refreshed_odds = await db.execute(
                        select(Odds).where(
                            Odds.match_id == req.match_id,
                            Odds.bet_type == _BTEnum(bt),
                            Odds.provider == provider_label,
                            Odds.valid_to.is_(None),
                        )
                    )
                    odds_obj = refreshed_odds.scalar_one_or_none()
                    if match is not None and odds_obj is not None:
                        raw_odds = (odds_obj.odds_data or {}).get(sel)
                        if raw_odds is None or isinstance(raw_odds, (dict, list)):
                            raise HTTPException(
                                status_code=409,
                                detail="自动同步后未获取到可下注的小球赔率",
                            )
                        current_odds = float(raw_odds)
                        fresh_at = match.updated_at or odds_obj.valid_from
            except HTTPException:
                raise
            except Exception as e:
                logger.warning("滚球赔率缺少时间，自动同步失败 user=%s: %s", current_user.id, e)
            if fresh_at is None:
                raise HTTPException(
                    status_code=409,
                    detail="滚球赔率时间缺失，自动重新同步后仍不可用，请稍后重试",
                )
        if getattr(fresh_at, "tzinfo", None) is None:
            fresh_at = fresh_at.replace(tzinfo=timezone.utc)
        odds_age = (datetime.now(timezone.utc) - fresh_at).total_seconds()
        max_age = max(1, int(settings.LIVE_ODDS_MAX_AGE_SEC))
        if odds_age < -5 or odds_age > max_age:
            raise HTTPException(
                status_code=409,
                detail=f"滚球赔率已过期（{max(0, int(odds_age))}秒），请刷新盘口后再下单",
            )

    # 提前加载策略，用于赔率变动判断
    from app.ai.strategy import load_fresh_strategy
    from app.ai.strategy_gates import stake_bounds

    _, strat = await load_fresh_strategy(current_user.id)
    min_odds = float(strat.min_odds)

    if abs(current_odds - float(req.odds)) > 0.05:
        if current_odds + 1e-9 < min_odds:
            raise HTTPException(
                status_code=400,
                detail=f"赔率已变动，当前 {req.selection} 赔率为 {current_odds}，低于最低赔率 {min_odds}",
            )
        # 赔率变动但不低于最低赔率 -> 直接用当前赔率下单
        final_odds = current_odds
    else:
        final_odds = float(req.odds)

    # 3. 验证投注金额：严格按 AI 策略「单笔最大金额」
    lo, hi = stake_bounds(strat)
    if req.stake < lo:
        raise HTTPException(status_code=400, detail=f"金额需 ≥{lo:g}（AI 策略配置）")
    if req.stake > hi:
        raise HTTPException(
            status_code=400,
            detail=f"金额超过策略单笔上限 {hi:g}，请在「AI 策略配置」中调整",
        )
    if req.stake > settings.MAX_BET_AMOUNT:
        raise HTTPException(status_code=400, detail=f"最大投注金额: {settings.MAX_BET_AMOUNT}")

    # === 仅允许真实站点下单（禁止本地演示账本）===
    external_bet_id = None
    site_acc = None
    placed_on_site = False
    if not provider_code:
        raise HTTPException(
            status_code=400,
            detail="请指定投注站点（OB / 平博），禁止本地演示下单",
        )

    acc_res = await db.execute(
        select(BookmakerAccount).where(
            BookmakerAccount.user_id == current_user.id,
            BookmakerAccount.code == provider_code,
            BookmakerAccount.enabled.is_(True),
        )
    )
    site_acc = acc_res.scalar_one_or_none()

    from app.services.bookmakers.registry import is_real_live_account

    if not site_acc or not is_real_live_account(provider_code, site_acc.base_url or ""):
        raise HTTPException(
            status_code=400,
            detail=f"请先在站点配置中填写 {provider_label or provider_code} 真实网址并验证连接",
        )
    if site_acc.status != BookmakerStatus.CONNECTED:
        raise HTTPException(status_code=400, detail=f"请先连接{provider_label or provider_code}站点")
    if float(site_acc.balance or 0) < float(req.stake):
        raise HTTPException(
            status_code=400,
            detail=f"{provider_label or provider_code} 余额不足（当前 {float(site_acc.balance or 0):.2f}）",
        )

    connector = get_connector(
        provider_code,
        base_url=site_acc.base_url,
        username=site_acc.username,
        password=decrypt_secret(site_acc.password_encrypted),
        balance=site_acc.balance,
        session_token=decrypt_secret(site_acc.session_token_encrypted),
        profile=site_acc.profile_json if isinstance(site_acc.profile_json, dict) else {},
    )
    # 跨站合并：优先用该站在 extra_data.ids 中的 external_id
    ids = dict((match.extra_data or {}).get("ids") or {})
    ext_id = str(
        ids.get(provider_code)
        or match.external_id
        or ""
    )
    if not ext_id or ext_id.startswith("local:") or ext_id.startswith("demo:"):
        raise HTTPException(status_code=400, detail="缺少真实站点赛事 ID，请先同步滚球盘口")

    # Gate 下单可能 60–120s：先释放 DB 事务，避免 idle_in_transaction 杀连接后 500
    from app.services.bookmakers.sync_session import release_db_session

    place_payload = {
        "match_external_id": ext_id,
        "selection": req.selection,
        "odds": final_odds,
        "stake": req.stake,
        "bet_type": req.bet_type if isinstance(req.bet_type, str) else str(req.bet_type),
        "odds_data": dict(odds_obj.odds_data or {}),
    }
    # 真实站点可能在投注单中返回更高的最低投注额。允许平博在“本场动态
    # 仓位”与“单笔最大金额”之间安全调整，并把余额作为第三道硬上限。
    place_payload["odds_data"]["_stake_policy"] = {
        "dynamic_stake": str(req.stake),
        "max_stake": str(hi),
        "available_balance": str(site_acc.balance or 0),
    }
    # 队名注入 odds_data：UI 下单依赖队名定位赛事行
    place_payload["odds_data"]["_home_team"] = match.home_team or ""
    place_payload["odds_data"]["_away_team"] = match.away_team or ""
    match_home = match.home_team
    match_away = match.away_team
    match_id_val = match.id
    user_id_val = current_user.id
    acc_id_val = site_acc.id
    stake_val = req.stake
    odds_val = final_odds
    potential_payout = (
        Decimal(str(stake_val)) * Decimal(str(odds_val))
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    selection_val = req.selection
    bet_type_val = place_payload["bet_type"]
    # 结算用盘口线
    line_val = None
    try:
        bt = str(bet_type_val).lower()
        if bt == "total" and odds_obj.total is not None:
            line_val = float(odds_obj.total)
        elif bt == "spread" and odds_obj.spread is not None:
            line_val = float(odds_obj.spread)
        elif isinstance(odds_obj.odds_data, dict) and odds_obj.odds_data.get("line") is not None:
            line_val = float(odds_obj.odds_data.get("line"))
    except Exception:
        line_val = None

    await release_db_session(db)

    # 同一用户的下单串行化：限额检查、同场防重、站点提交和落库必须属于同一个
    # 临界区，否则不同赛事的并发请求可越过每日限额，或在落库前重复提交。
    from app.core.cache import cache
    import uuid

    lock_token = uuid.uuid4().hex
    lock_key = f"bet:place:user:{user_id_val}"
    got_lock = await cache.acquire_lock(lock_key, ttl_sec=_BET_LOCK_TTL_SEC, token=lock_token)
    if not got_lock:
        raise HTTPException(status_code=409, detail="当前账户正在下单中，请稍候")
    lock_renew_task = asyncio.create_task(
        _renew_bet_lock(lock_key, lock_token), name=f"ob-bet-lock:{user_id_val}"
    )

    try:
        # 抢锁后再检查每日限额和同场未结算单，避免跨 worker 并发穿透。
        async with AsyncSessionLocal() as cdb:
            today_start = today_start_utc()
            daily_count_result = await cdb.execute(
                select(func.count(Bet.id)).where(
                    Bet.user_id == user_id_val,
                    Bet.created_at >= today_start,
                )
            )
            daily_count = daily_count_result.scalar_one()
            if daily_count >= settings.MAX_DAILY_BETS:
                raise HTTPException(
                    status_code=400,
                    detail=f"今日投注已达上限({settings.MAX_DAILY_BETS}笔)",
                )
            again = await cdb.execute(
                select(Bet.id).where(
                    Bet.user_id == user_id_val,
                    Bet.match_id.in_(sib_ids or [match_id_val]),
                    Bet.status == BetStatus.SUCCESS,
                    Bet.settled_at.is_(None),
                ).limit(1)
            )
            await cdb.commit()
            if again.scalar_one_or_none() is not None:
                raise HTTPException(
                    status_code=400,
                    detail="该场比赛已有未结算注单（OB/平博同场只投一单）",
                )

        place = await connector.place_bet(
            match_external_id=place_payload["match_external_id"],
            selection=place_payload["selection"],
            odds=place_payload["odds"],
            stake=place_payload["stake"],
            bet_type=place_payload["bet_type"],
            odds_data=place_payload["odds_data"],
        )
        if not place.ok:
            raise HTTPException(status_code=400, detail=place.message or f"{provider_label}下单失败")
        external_bet_id = place.external_bet_id
        actual_stake = Decimal(str(place.actual_stake or 0))
        if actual_stake > 0:
            stake_val = actual_stake.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            place_payload["stake"] = stake_val
            potential_payout = (
                stake_val * Decimal(str(odds_val))
            ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        if provider_code == "ob" and external_bet_id:
            logger.info("OB 下单回执已返回 orderNo=%s，按成功受理，不再做存在性验证", external_bet_id)

        provider_label = provider_name(provider_code)
        placed_on_site = True
    finally:
        # 站点拒绝、超时等没有产生本地成功单时可立即放锁；站点已受理则
        # 必须继续持有到本地订单事务提交完毕。
        if not placed_on_site:
            lock_renew_task.cancel()
            try:
                await lock_renew_task
            except asyncio.CancelledError:
                pass
            await cache.release_lock(lock_key, lock_token)

    # 4. 必须落到真实站点，否则拒绝（不扣本地余额）
    if not placed_on_site:
        raise HTTPException(status_code=400, detail="下单未落到真实站点")

    # 5–6. 短会话写库（余额/注单/流水）。无论落库成功或失败，都在此处
    # 结束用户锁；失败时至少会在响应前留下明确错误而不是制造并发重复提交。
    try:
        async with AsyncSessionLocal() as wdb:
            site_acc = await wdb.get(BookmakerAccount, acc_id_val)
            user_row = await wdb.get(User, user_id_val)
            if site_acc is None or user_row is None:
                raise HTTPException(status_code=500, detail="下单成功但写库失败：账户丢失")
            try:
                bal = await connector.fetch_balance()
                site_acc.balance = bal
            except Exception:
                if place.balance_after and place.balance_after > 0:
                    site_acc.balance = place.balance_after
                else:
                    site_acc.balance = Decimal(str(site_acc.balance or 0)) - stake_val

            bet = Bet(
                user_id=user_id_val,
                match_id=match_id_val,
                bet_type=bet_type_val,
                selection=selection_val,
                odds=odds_val,
                stake=stake_val,
                potential_payout=potential_payout,
                actual_payout=Decimal("0"),  # 未结算，由 bet_settlement 按完场比分写回
                line=line_val,
                status=BetStatus.SUCCESS,
                provider=provider_label,
                external_bet_id=external_bet_id,
            )
            wdb.add(bet)
            await wdb.flush()

            tx = Transaction(
                user_id=user_id_val,
                type=TransactionType.BET_PLACE,
                amount=Decimal("0"),
                balance_after=user_row.balance,
                bet_id=bet.id,
                description=(
                    f"站点投注: {match_home} vs {match_away} [{selection_val} @ {odds_val}]"
                    f" @{provider_label}"
                    + (f" 单号={external_bet_id}" if external_bet_id else "")
                ),
            )
            wdb.add(tx)
            await wdb.commit()
            await wdb.refresh(bet)
            balance_after_user = user_row.balance
            bet_id_val = bet.id
            bet_status_val = bet.status.value if hasattr(bet.status, "value") else str(bet.status)
    finally:
        lock_renew_task.cancel()
        try:
            await lock_renew_task
        except asyncio.CancelledError:
            pass
        await cache.release_lock(lock_key, lock_token)

    # 8. WebSocket推送
    await manager.broadcast_to_user(user_id_val, {
        "type": WSEventType.BET_PLACED,
        "data": {
            "bet_id": bet_id_val,
            "action": "placed",
            "stake": float(stake_val),
            "odds": odds_val,
            "potential_payout": float(potential_payout),
            "balance": float(balance_after_user),
            "external_bet_id": external_bet_id,
            "provider": provider_label,
        }
    })

    logger.info(
        f"投注成功: user={user_id_val}, match={match_id_val}, stake={stake_val}, "
        f"odds={odds_val}, provider={provider_label}, ext={external_bet_id}"
    )

    return APIResponse(
        message="已提交真实站点投注",
        data=PlaceBetResponse(
            bet_id=bet_id_val,
            status=bet_status_val,
            stake=stake_val,
            odds=odds_val,
            potential_payout=potential_payout,
            balance_after=balance_after_user,
        ).model_dump() | {"external_bet_id": external_bet_id, "provider": provider_label}
    )


# === 投注历史 ===
@router.get("/history", response_model=APIResponse)
async def bet_history(
    status: Optional[str] = None,
    status_filter: Optional[str] = None,
    provider: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """查询投注历史：仅返回本地手动/自动下单成功记录。"""
    pending_items = await _load_pending_items_for_view(current_user.id, scene="投注记录")

    sf = status_filter or status
    if sf:
        sf_norm = str(sf).strip().lower()
        if sf_norm not in {"success", BetStatus.SUCCESS.value}:
            return APIResponse(data={
                "items": [],
                "total": 0,
                "page": page,
                "page_size": page_size,
                "remote_fallback": False,
                "remote_status": "disabled",
            })
    async with AsyncSessionLocal() as qdb:
        query = (
            select(Bet)
            .where(
                Bet.user_id == current_user.id,
                Bet.status.in_(tuple(LOCAL_RECORDED_BET_STATUSES)),
            )
            .options(selectinload(Bet.match))
        )
        count_query = select(func.count(Bet.id)).where(
            Bet.user_id == current_user.id,
            Bet.status.in_(tuple(LOCAL_RECORDED_BET_STATUSES)),
        )

        if provider:
            pc = provider.strip().lower()
            if pc == "ob":
                query = query.where(Bet.provider.ilike("%OB%"))
                count_query = count_query.where(Bet.provider.ilike("%OB%"))
            elif pc == "pinnacle":
                from sqlalchemy import or_

                pin_f = or_(Bet.provider.ilike("%平博%"), Bet.provider.ilike("%pinnacle%"))
                query = query.where(pin_f)
                count_query = count_query.where(pin_f)

        if start_date:
            sd = datetime.fromisoformat(start_date)
            query = query.where(Bet.created_at >= sd)
            count_query = count_query.where(Bet.created_at >= sd)

        if end_date:
            ed = datetime.fromisoformat(end_date)
            query = query.where(Bet.created_at <= ed)
            count_query = count_query.where(Bet.created_at <= ed)

        total = (await qdb.execute(count_query)).scalar_one()

        offset = (page - 1) * page_size
        result = await qdb.execute(
            query.order_by(Bet.created_at.desc()).offset(offset).limit(page_size)
        )
        bets = result.scalars().all()

    items = []
    for b in bets:
        prov = b.provider or ""
        prov_code = "ob" if ("OB" in prov or "开云" in prov) else (
            "pinnacle" if ("平博" in prov or "pinnacle" in prov.lower()) else ""
        )
        items.append({
            "id": b.id,
            "match_id": b.match_id,
            "match_info": f"{b.match.league} / {b.match.home_team} vs {b.match.away_team}" if b.match else f"Match #{b.match_id}",
            "status": b.status.value if hasattr(b.status, 'value') else str(b.status),
            "provider": prov,
            "provider_code": prov_code,
            "created_at": b.created_at.isoformat() if b.created_at else None,
            "pending": False,
        })

    items = pending_items + items
    total = int(total or 0) + len(pending_items)
    items = items[:page_size]

    return APIResponse(data={
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "remote_fallback": False,
        "remote_status": "disabled",
    })



# === 持仓概览 ===
@router.get("/portfolio", response_model=APIResponse)
async def portfolio_summary(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """持仓概览统计：仅统计本地下单成功记录。"""
    pending_items = await _load_pending_items_for_view(current_user.id, scene="持仓概览")

    today_start = today_start_utc()
    month_start_dt = month_start_utc()
    pending_today = 0
    pending_month = 0
    for item in pending_items:
        created_at = item.get("created_at")
        try:
            dt = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
        except Exception:
            dt = None
        if not dt:
            continue
        if dt >= today_start:
            pending_today += 1
        if dt >= month_start_dt:
            pending_month += 1

    async with AsyncSessionLocal() as qdb:
        today_bets_result = await qdb.execute(
            select(func.count(Bet.id)).where(
                Bet.user_id == current_user.id,
                Bet.status.in_(tuple(LOCAL_RECORDED_BET_STATUSES)),
                Bet.created_at >= today_start
            )
        )
        today_bets = today_bets_result.scalar_one()

        monthly_bets_result = await qdb.execute(
            select(func.count(Bet.id)).where(
                Bet.user_id == current_user.id,
                Bet.status.in_(tuple(LOCAL_RECORDED_BET_STATUSES)),
                Bet.created_at >= month_start_dt
            )
        )
        monthly_bets = monthly_bets_result.scalar_one()

    total_assets = 0.0
    pnl_info = {
        "total_assets": 0.0,
        "balance_delta": 0.0,
        "balance_change": 0.0,
        "reference_balance": 0.0,
        "previous_balance": 0.0,
        "snapshot_updated_at": None,
        "pnl_mode": "site_balance_delta",
    }
    risk_pnl = 0.0
    try:
        from app.services.balances import load_site_balances
        from app.services.daily_pnl import get_daily_pnl, sync_balance_snapshot

        async with AsyncSessionLocal() as bdb:
            site_balances = await load_site_balances(bdb, current_user.id)
        total_assets = sum(float(s.get("balance") or 0) for s in site_balances)
        pnl_info = await sync_balance_snapshot(current_user.id, total_assets)
        risk_daily = await get_daily_pnl(current_user.id, total_assets)
        risk_pnl = float(risk_daily.get("daily_pnl") or 0)
    except Exception as e:
        logger.warning("持仓概览余额/盈亏统计失败 user=%s: %s", current_user.id, e)

    return APIResponse(data={
        "today_bets": int(today_bets or 0) + pending_today,
        "total_bets": int(monthly_bets or 0) + pending_month,
        "confirmed_today_bets": int(today_bets or 0),
        "confirmed_total_bets": int(monthly_bets or 0),
        "pending_bets": len(pending_items),
        "total_assets": pnl_info["total_assets"],
        # 口径说明：daily_pnl = 当日基线差（risk_daily_pnl 同源）；
        # balance_delta = 相对长期基准（工作台「网站盈亏」卡片）的累计增减。
        "daily_pnl": risk_pnl,
        "balance_delta": pnl_info["balance_delta"],
        "balance_change": pnl_info["balance_change"],
        "baseline": pnl_info["reference_balance"],
        "reference_balance": pnl_info["reference_balance"],
        "previous_balance": pnl_info["previous_balance"],
        "snapshot_updated_at": pnl_info["snapshot_updated_at"],
        "pnl_mode": pnl_info["pnl_mode"],
        "risk_daily_pnl": risk_pnl,
        "remote_fallback": False,
        "remote_status": "disabled",
    })


@router.post("/portfolio/reset-pnl", response_model=APIResponse)
async def reset_pnl(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """手动清零复位盈亏基线。

    以当前站点总余额重建基线快照与日基线：
    - 工作台「网站盈亏」立即归零
    - 风控日盈亏（risk_daily_pnl / 止损止盈）从当前余额重新起算
    适用于：充值后基线漂移、历史测试数据污染、想从今天重新计盈亏。
    """
    try:
        from app.services.balances import load_site_balances
        from app.services.daily_pnl import reset_pnl_baseline

        site_balances = await load_site_balances(db, current_user.id)
        total_assets = sum(float(s.get("balance") or 0) for s in site_balances)
        pnl_info = await reset_pnl_baseline(current_user.id, total_assets)
    except Exception as e:
        logger.exception("盈亏复位失败 user=%s", current_user.id)
        raise HTTPException(status_code=500, detail=f"盈亏复位失败: {e}")

    return APIResponse(data={
        "total_assets": pnl_info["total_assets"],
        "balance_delta": 0.0,
        "daily_pnl": 0.0,
        "balance_change": 0.0,
        "reference_balance": pnl_info["reference_balance"],
        "snapshot_updated_at": pnl_info["snapshot_updated_at"],
        "pnl_mode": pnl_info["pnl_mode"],
        "message": f"盈亏已清零复位（新基准 ¥{pnl_info['reference_balance']:.2f}）",
    })
