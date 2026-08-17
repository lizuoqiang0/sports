"""
赔率 API - WebSocket实时订阅 / 历史查询
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.models.user import Match, Odds
from app.core.security import decode_token, TokenType, get_current_user
from app.core.websocket import WSEventType, manager
from app.core.cache import cache
from app.schemas import APIResponse
from app.models.user import User

logger = logging.getLogger(__name__)
router = APIRouter(tags=["赔率"])


# === REST: 获取赛事赔率 ===
@router.get("/api/v1/matches/{match_id}/odds", response_model=APIResponse)
async def get_match_odds(
    match_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """获取赛事当前赔率（始终返回数组）"""
    # 先查缓存（兼容历史 {items:[...]} 与直接数组两种形态）
    cached = await cache.get_cached_odds(match_id)
    if cached is not None:
        if isinstance(cached, list):
            return APIResponse(data=cached)
        if isinstance(cached, dict) and isinstance(cached.get("items"), list):
            return APIResponse(data=cached["items"])

    result = await db.execute(
        select(Odds).where(Odds.match_id == match_id, Odds.valid_to.is_(None))
    )
    odds_list = result.scalars().all()

    data = [
        {
            "id": o.id,
            "bet_type": o.bet_type.value if hasattr(o.bet_type, 'value') else str(o.bet_type),
            "odds_data": o.odds_data,
            "spread": o.spread,
            "total": o.total,
            "provider": o.provider,
            "is_live": o.is_live,
            "valid_from": o.valid_from.isoformat() if o.valid_from else None,
        }
        for o in odds_list
    ]

    # 缓存10秒：与 API 响应同形（数组），避免前端 odds.find 崩溃
    await cache.set_json(f"odds:match:{match_id}", data, ttl=10)

    return APIResponse(data=data)


# === WebSocket: 赔率实时订阅 ===
@router.websocket("/ws/odds")
async def odds_websocket(websocket: WebSocket, token: Optional[str] = None):
    """
    WebSocket端点 - 实时赔率推送

    客户端消息协议:
    {
        "action": "subscribe" | "unsubscribe" | "subscribe_match",
        "channel": "odds:match:123" | "odds:live" | "odds:sport:football",
        "match_id": 123  (可选)
    }

    服务端推送:
    {
        "type": "odds_update",
        "channel": "odds:match:123",
        "data": {
            "match_id": 123,
            "bet_type": "moneyline",
            "odds_data": {"home": 1.85, "away": 2.10},
            "timestamp": "2024-01-01T12:00:00Z"
        }
    }
    """
    # Token鉴权
    if not token:
        await websocket.close(code=4001, reason="缺少Token")
        return

    try:
        payload = decode_token(token)
        if payload.get("type") != TokenType.ACCESS.value:
            await websocket.close(code=4001, reason="Token类型错误")
            return
        user_id = int(payload.get("sub"))
    except Exception:
        await websocket.close(code=4001, reason="Token无效")
        return

    # 连接管理
    await manager.connect(websocket, user_id)

    try:
        while True:
            # 接收客户端消息
            message = await websocket.receive_json()

            action = message.get("action")
            channel = message.get("channel")
            match_id = message.get("match_id")

            if action == "subscribe":
                if match_id:
                    channel = f"odds:match:{match_id}"
                elif not channel:
                    channel = "odds:live"  # 默认订阅全部直播赔率

                await manager.subscribe(websocket, channel)
                logger.debug(f"用户{user_id}订阅: {channel}")

                # 立即推送当前赔率快照
                if match_id:
                    snapshot = await _get_odds_snapshot(match_id)
                    if snapshot:
                        await websocket.send_json({
                            "type": "snapshot",
                            "channel": channel,
                            "data": snapshot,
                            "timestamp": datetime.now(timezone.utc).isoformat()
                        })

            elif action == "unsubscribe":
                if match_id:
                    channel = f"odds:match:{match_id}"
                await manager.unsubscribe(websocket, channel)

            elif action == "subscribe_sport":
                sport = message.get("sport", "football")
                channel = f"odds:sport:{sport}"
                await manager.subscribe(websocket, channel)

            elif action == "ping":
                await websocket.send_json({"type": "pong", "timestamp": datetime.now(timezone.utc).isoformat()})

            else:
                await websocket.send_json({"type": "error", "message": f"未知操作: {action}"})

    except WebSocketDisconnect:
        await manager.disconnect(websocket, user_id)
    except Exception as e:
        logger.error(f"WS error: {e}")
        await manager.disconnect(websocket, user_id)


# === 辅助方法 ===
async def _get_odds_snapshot(match_id: int) -> Optional[dict]:
    """获取赔率快照"""
    cached = await cache.get_cached_odds(match_id)
    if cached:
        return cached

    # 从数据库读取 (需要独立session)
    # 这里简化为返回None，实际应创建新的db session
    return None


# === REST: 赔率历史 ===
@router.get("/api/v1/odds/history/{match_id}", response_model=APIResponse)
async def get_odds_history(
    match_id: int,
    bet_type: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """查询赔率/盘口版本历史（初盘→变盘）。"""
    from app.ai.market_recommend import (
        _normalize_bet_type,
        load_market_line_movement,
        load_odds_history_rows,
    )

    history = await load_odds_history_rows(
        db, match_id, bet_type=bet_type, limit=limit
    )
    want = {_normalize_bet_type(bet_type)} if bet_type else {"moneyline", "spread", "total"}
    movements = {}
    for bt in ("moneyline", "spread", "total"):
        if bt not in want:
            continue
        move = await load_market_line_movement(db, match_id, bt)
        if move:
            movements[bt] = move
    return APIResponse(data={
        "match_id": match_id,
        "history": history,
        "line_movements": movements,
        "odds_style": "asian",
    })
