from __future__ import annotations

import logging
import asyncio
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

import httpx
from sqlalchemy import select

from app.core.cache import cache
from app.core.crypto import decrypt_secret
from app.database import AsyncSessionLocal
from app.models.user import AIConfig, Bet, BetStatus, BetType, BookmakerAccount, BookmakerStatus, Match, Transaction, TransactionType, User
from app.services.bookmakers.gate_client import _gate_headers, post_login

logger = logging.getLogger(__name__)


async def _ensure_cache_connected() -> None:
    try:
        _ = cache.client
    except RuntimeError:
        await cache.connect()


def _parse_created_at(value) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    raw = str(value or "").strip()
    if not raw:
        return datetime.now(timezone.utc)
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return datetime.now(timezone.utc)


def _bet_type_of(value) -> BetType:
    raw = str(value or "").strip().lower()
    if raw == BetType.MONEYLINE.value:
        return BetType.MONEYLINE
    if raw == BetType.SPREAD.value:
        return BetType.SPREAD
    return BetType.TOTAL


def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _to_decimal(value, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except Exception:
        return Decimal(default).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


async def _load_prediction_fallback(match_id: int) -> dict:
    data = await cache.get_json(f"ai:prediction:{int(match_id)}")
    return data if isinstance(data, dict) else {}


def _resolve_pending_context(
    *,
    order: dict,
    pending: dict,
    prediction: dict,
    default_stake: Decimal,
) -> dict:
    selection = str(
        order.get("selection")
        or pending.get("selection")
        or prediction.get("prediction")
        or "under"
    ).strip().lower()
    bet_type_raw = (
        order.get("bet_type")
        or pending.get("bet_type")
        or prediction.get("bet_type")
        or BetType.TOTAL.value
    )
    bet_type = _bet_type_of(bet_type_raw)
    odds = _to_float(
        order.get("odds")
        or pending.get("odds")
        or prediction.get("odds")
        or 0
    )
    stake = _to_decimal(order.get("stake") or pending.get("stake") or 0)
    if stake <= 0:
        stake = default_stake
    line_raw = order.get("line")
    if line_raw is None:
        line_raw = pending.get("line")
    line = _to_float(line_raw, default=0.0) if line_raw is not None else None
    confidence = _to_float(
        pending.get("confidence")
        or prediction.get("confidence")
        or 0
    )
    if 0 < confidence <= 1:
        confidence *= 100.0
    reasoning = str(
        pending.get("reasoning")
        or "OB 待定成功单自动补录"
    ).strip() or "OB 待定成功单自动补录"
    return {
        "selection": selection,
        "bet_type": bet_type,
        "odds": odds,
        "stake": stake,
        "line": line,
        "confidence": confidence or None,
        "reasoning": reasoning,
        "provider": str(order.get("provider") or pending.get("provider") or "OB体育"),
    }


async def _materialize_pending_success(
    *,
    db,
    user_row,
    user_id: int,
    key: str,
    pending: dict,
    order: dict,
    default_stake: Decimal,
) -> bool:
    order_no = str((pending or {}).get("order_no") or order.get("external_bet_id") or "").strip()
    if not order_no:
        return False

    existing = (
        await db.execute(
            select(Bet).where(
                Bet.user_id == int(user_id),
                Bet.external_bet_id == order_no,
            )
        )
    ).scalar_one_or_none()
    if existing:
        await cache.delete(key)
        return False

    try:
        match_id = int(str(key).rsplit(":", 1)[-1])
    except Exception:
        return False
    match = await db.get(Match, match_id)
    if not match:
        return False

    prediction = await _load_prediction_fallback(match_id)
    resolved = _resolve_pending_context(
        order=order,
        pending=pending,
        prediction=prediction,
        default_stake=default_stake,
    )
    stake = resolved["stake"]
    odds = resolved["odds"]
    if stake <= 0 or odds <= 0:
        logger.warning(
            "待定单转成功跳过：订单字段无效 orderNo=%s stake=%s odds=%s pending=%s prediction=%s",
            order_no,
            stake,
            odds,
            pending,
            prediction,
        )
        return False

    created_at = _parse_created_at(order.get("created_at") or pending.get("time"))
    potential_payout = (stake * Decimal(str(odds))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    bet = Bet(
        user_id=int(user_id),
        match_id=match_id,
        bet_type=resolved["bet_type"],
        selection=resolved["selection"],
        odds=odds,
        stake=stake,
        potential_payout=potential_payout,
        actual_payout=Decimal("0"),  # 未结算，由 bet_settlement 按完场比分写回
        line=resolved["line"],
        provider=resolved["provider"],
        status=BetStatus.SUCCESS,
        is_ai_bet=True,
        ai_confidence=resolved["confidence"],
        ai_reasoning=resolved["reasoning"],
        external_bet_id=order_no,
        created_at=created_at,
        updated_at=created_at,
    )
    db.add(bet)
    await db.flush()

    tx = Transaction(
        user_id=int(user_id),
        type=TransactionType.AI_BET,
        amount=Decimal("0"),
        balance_after=Decimal(str(user_row.balance or 0)),
        bet_id=bet.id,
        description=(
            f"AI补录成功单: {match.home_team} vs {match.away_team} "
            f"[{bet.selection} @ {odds}] 单号={order_no}"
        ),
        created_at=created_at,
    )
    db.add(tx)
    await cache.delete(key)
    return True


async def _fetch_ob_orders(site_acc, *, days: int = 1) -> list[dict]:
    from app.config import settings

    gate = (settings.BOOKMAKER_BROWSER_GATE_URL or "").rstrip("/")
    if not gate or not getattr(site_acc, "session_token_encrypted", ""):
        return []

    try:
        session_token = decrypt_secret(site_acc.session_token_encrypted)
    except Exception as e:
        logger.warning("待定单补录失败：解密 OB session 失败: %s", e)
        return []

    busy_retries = 6
    busy_interval = 2.0

    async def _request_once() -> tuple[list[dict], dict]:
        async with httpx.AsyncClient(timeout=45.0, headers=_gate_headers()) as client:
            resp = await client.post(
                f"{gate}/bets/history",
                json={
                    "site_code": "ob",
                    "base_url": site_acc.base_url or "",
                    "session_token": session_token,
                    "days": max(1, int(days or 1)),
                },
            )
            data = resp.json() if resp.status_code < 500 and resp.content else {}
            orders = data.get("orders") or []
            return [row for row in orders if isinstance(row, dict)], (data if isinstance(data, dict) else {})

    async def _request_with_busy_retry() -> tuple[list[dict], dict]:
        last_meta: dict = {}
        for attempt in range(1, busy_retries + 1):
            orders, meta = await _request_once()
            last_meta = meta
            if orders:
                return orders, meta
            if not bool((meta or {}).get("busy")):
                return orders, meta
            logger.info(
                "待定单补录：OB 历史繁忙 attempt=%s/%s msg=%s",
                attempt,
                busy_retries,
                (meta or {}).get("message") or "",
            )
            if attempt < busy_retries:
                await asyncio.sleep(busy_interval)
        return [], last_meta

    async def _reopen_gate_session() -> bool:
        try:
            password = decrypt_secret(getattr(site_acc, "password_encrypted", "") or "")
        except Exception as e:
            logger.warning("待定单补录失败：解密 OB 密码失败: %s", e)
            return False
        try:
            result = await post_login(
                base_url=site_acc.base_url or "",
                username=getattr(site_acc, "username", "") or "",
                password=password,
                session_token=session_token,
                site_code="ob",
                wait_seconds=25,
                force_new=False,
                manual_venue=False,
                timeout=120.0,
            )
            ok = bool((result or {}).get("ok"))
            if not ok:
                logger.warning("待定单补录：重建 OB 长连接失败: %s", (result or {}).get("message"))
                return False
            await asyncio.sleep(6.0)
            return True
        except Exception as e:
            logger.warning("待定单补录：重建 OB 长连接异常: %s", e)
            return False

    try:
        orders, meta = await _request_with_busy_retry()
        if orders:
            return orders
        message = str((meta or {}).get("message") or "")
        need_reopen = (
            (not bool((meta or {}).get("ok")) and not bool((meta or {}).get("busy")))
            or ("无有效长连接" in message)
            or ("先验证并进入场馆" in message)
        )
        if need_reopen:
            reopened = await _reopen_gate_session()
            if reopened:
                orders, _ = await _request_with_busy_retry()
                if orders:
                    logger.info("待定单补录：重建长连接后成功拉到 OB 注单 count=%s", len(orders))
                    return orders
        return []
    except Exception as e:
        logger.warning("待定单补录失败：拉取 OB 注单历史异常: %s", e)
        return []


async def reconcile_pending_ai_bets(user_id: int) -> int:
    """把已有 orderNo 的待定 AI 单直接补录为本地成功单。"""
    try:
        await _ensure_cache_connected()
        keys = [k async for k in cache.client.scan_iter(match=f"ai:bet:pending:{int(user_id)}:*")]
    except Exception as e:
        logger.warning("扫描待定注单失败 user=%s: %s", user_id, e)
        return 0

    if not keys:
        return 0

    pending_map: dict[str, dict] = {}
    for key in keys:
        pending = await cache.get_json(key)
        if isinstance(pending, dict):
            pending_map[str(key)] = pending

    async with AsyncSessionLocal() as db:
        ai_cfg = (
            await db.execute(select(AIConfig).where(AIConfig.user_id == int(user_id)))
        ).scalar_one_or_none()

    default_stake = _to_decimal(getattr(ai_cfg, "max_bet_amount", 0) or 0)

    async with AsyncSessionLocal() as db:
        user_row = await db.get(User, int(user_id))
        if not user_row:
            return 0

        recovered = 0
        for key in keys:
            pending = pending_map.get(str(key)) or {}
            order_no = str((pending or {}).get("order_no") or "").strip()
            if not order_no:
                continue
            order = {
                "external_bet_id": order_no,
                "created_at": pending.get("time"),
                "selection": pending.get("selection"),
                "bet_type": pending.get("bet_type"),
                "odds": pending.get("odds"),
                "stake": pending.get("stake"),
                "line": pending.get("line"),
                "provider": pending.get("provider") or "OB体育",
            }
            recovered += int(
                await _materialize_pending_success(
                    db=db,
                    user_row=user_row,
                    user_id=int(user_id),
                    key=str(key),
                    pending=pending,
                    order=order,
                    default_stake=default_stake,
                )
            )

        if recovered:
            logger.info("待定 AI 注单自动补录完成 user=%s recovered=%s", user_id, recovered)
        await db.commit()
        return recovered


async def list_pending_ai_bets(user_id: int) -> list[dict]:
    """读取仍未补录成功的待定 AI 注单，供持仓页降级展示。"""
    try:
        await _ensure_cache_connected()
        keys = [k async for k in cache.client.scan_iter(match=f"ai:bet:pending:{int(user_id)}:*")]
    except Exception as e:
        logger.warning("读取待定注单失败 user=%s: %s", user_id, e)
        return []

    items: list[dict] = []
    async with AsyncSessionLocal() as db:
        for key in keys:
            pending = await cache.get_json(key)
            order_no = str((pending or {}).get("order_no") or "").strip()
            if not order_no:
                continue
            try:
                match_id = int(str(key).rsplit(":", 1)[-1])
            except Exception:
                continue

            existing = (
                await db.execute(
                    select(Bet.id).where(
                        Bet.user_id == int(user_id),
                        Bet.external_bet_id == order_no,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                continue

            match = await db.get(Match, match_id)
            created_at = _parse_created_at((pending or {}).get("time"))
            items.append(
                {
                    "id": f"pending-{match_id}-{order_no}",
                    "match_id": match_id,
                    "match_info": (
                        f"{match.league} / {match.home_team} vs {match.away_team}"
                        if match else f"Match #{match_id}"
                    ),
                    "status": "pending_confirm",
                    "provider": "OB体育",
                    "provider_code": "ob",
                    "created_at": created_at.isoformat(),
                    "external_bet_id": order_no,
                    "pending": True,
                }
            )

    items.sort(key=lambda x: str(x.get("created_at") or ""), reverse=True)
    return items
