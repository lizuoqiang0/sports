"""
博彩站点配置 API（仅 OB / 平博）
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal, get_db
from app.models.user import (
    User,
    BookmakerAccount,
    BookmakerStatus,
)
from app.core.security import get_current_user
from app.core.crypto import encrypt_secret, decrypt_secret
from app.schemas import (
    APIResponse,
    BookmakerBatchUpdateRequest,
    BookmakerVerifyBatchRequest,
)
from app.services.bookmakers.catalog import BOOKMAKER_CATALOG
from app.services.bookmakers.plugins.ob.kaiyun import is_demo_url
from app.services.bookmakers.gate_client import gate_url as _gate_url
from app.services.bookmakers.registry import get_connector, list_catalog
from app.services.bookmakers.sync import ensure_default_accounts, sync_user_bookmakers, sync_live_scores_odds
from app.services.provider_utils import compare_match_odds

logger = logging.getLogger(__name__)
router = APIRouter(tags=["双站 OB/平博"])


def _profile_summary(acc: BookmakerAccount) -> dict | None:
    raw = acc.profile_json
    if not raw:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return None
    if not isinstance(raw, dict) or not raw:
        return None
    return {
        "name": raw.get("name"),
        "member_id": raw.get("member_id"),
        "balance": raw.get("balance"),
    }


def _serialize_account(acc: BookmakerAccount) -> dict:
    from app.services.bookmakers.registry import is_real_live_account

    mode = "live" if is_real_live_account(acc.code, acc.base_url or "") else "demo"
    return {
        "code": acc.code,
        "name": acc.name,
        "base_url": acc.base_url,
        "username": acc.username,
        "has_password": bool(acc.password_encrypted),
        "has_session_token": bool(acc.session_token_encrypted),
        "status": acc.status.value if hasattr(acc.status, "value") else str(acc.status),
        "balance": float(acc.balance or 0),
        "enabled": acc.enabled,
        "last_sync_at": acc.last_sync_at.isoformat() if acc.last_sync_at else None,
        "last_error": acc.last_error,
        "mode": mode,
        "profile": _profile_summary(acc),
    }


@router.get("/api/v1/bookmakers/catalog", response_model=APIResponse)
async def bookmaker_catalog():
    return APIResponse(data=list_catalog())


@router.get("/api/v1/bookmakers/balances", response_model=APIResponse)
async def site_balances(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """获取站点余额汇总（OB / 平博）——主动刷新已连接站。"""
    from app.services.balances import load_site_balances

    sites = await load_site_balances(db, user.id)
    total = sum(float(s.get("balance") or 0) for s in sites)
    return APIResponse(data={
        "sites": sites,
        "total_balance": float(total),
    })


@router.get("/api/v1/bookmakers", response_model=APIResponse)
async def list_bookmakers(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    accounts = await ensure_default_accounts(db, user.id)
    # 重新查询保证顺序
    result = await db.execute(
        select(BookmakerAccount)
        .where(BookmakerAccount.user_id == user.id)
        .order_by(BookmakerAccount.id.asc())
    )
    rows = list(result.scalars().all())
    # 仅返回目录内站点（OB / 平博），忽略历史残留行
    order = list(BOOKMAKER_CATALOG.keys())
    rows = [a for a in rows if a.code in BOOKMAKER_CATALOG]
    rows.sort(key=lambda a: order.index(a.code) if a.code in order else 99)
    return APIResponse(data=[_serialize_account(a) for a in rows])


@router.put("/api/v1/bookmakers", response_model=APIResponse)
async def update_bookmakers(
    req: BookmakerBatchUpdateRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    await ensure_default_accounts(db, user.id)
    result = await db.execute(
        select(BookmakerAccount).where(BookmakerAccount.user_id == user.id)
    )
    by_code = {a.code: a for a in result.scalars().all()}

    from app.services.live_mode import reject_demo_url

    updated = []
    for item in req.accounts:
        if item.code not in BOOKMAKER_CATALOG:
            raise HTTPException(status_code=400, detail=f"未知站点: {item.code}")
        acc = by_code.get(item.code)
        if not acc:
            continue
        reject_demo_url(item.base_url)
        creds_changed = False
        if item.base_url.strip() != (acc.base_url or ""):
            creds_changed = True
        if item.username.strip() != (acc.username or ""):
            creds_changed = True
        acc.base_url = item.base_url.strip()
        acc.username = item.username.strip()
        acc.enabled = item.enabled
        if item.password:
            acc.password_encrypted = encrypt_secret(item.password)
            creds_changed = True
        if item.session_token is not None:
            token = item.session_token.strip()
            if token:
                acc.session_token_encrypted = encrypt_secret(token)
                creds_changed = True
            elif item.session_token == "":
                # 显式清空
                acc.session_token_encrypted = ""
                creds_changed = True
        if creds_changed and acc.status == BookmakerStatus.CONNECTED:
            acc.status = BookmakerStatus.DISCONNECTED
        updated.append(_serialize_account(acc))

    await db.flush()
    return APIResponse(message="配置已保存", data=updated)


@router.get("/api/v1/bookmakers/gate-health", response_model=APIResponse)
async def gate_health():
    """检测本机 Browser Gate 是否可达（Docker 经 host.docker.internal）。"""
    import httpx
    from app.services.bookmakers.gate_client import gate_url as _gate_url

    from app.services.bookmakers.gate_client import _gate_headers

    gate = _gate_url() or "http://host.docker.internal:9277"
    try:
        async with httpx.AsyncClient(timeout=3.0, headers=_gate_headers()) as client:
            r = await client.get(f"{gate}/health")
            if r.status_code == 200:
                body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
                return APIResponse(
                    message="浏览器网关正常",
                    data={"ok": True, "gate": gate, "health": body},
                )
            return APIResponse(
                success=False,
                message=f"浏览器网关异常 HTTP {r.status_code}",
                data={"ok": False, "gate": gate},
            )
    except Exception as e:
        return APIResponse(
            success=False,
            message="无法连接本机浏览器网关。请运行: bash scripts/ensure_browser_gate.sh start",
            data={"ok": False, "gate": gate, "error": str(e)},
        )


async def _verify_bookmaker_account(
    db: AsyncSession,
    user_id: int,
    code: str,
    manual_venue: bool = False,
) -> dict:
    """验证单个站点；成功返回 {message, account, profile}，失败抛 HTTPException。"""
    if code not in BOOKMAKER_CATALOG:
        raise HTTPException(status_code=404, detail="未知站点")

    from app.services.bookmakers.site_profiles import is_site_disabled

    if is_site_disabled(code):
        raise HTTPException(
            status_code=403,
            detail=f"{code.upper()} 登录已临时关闭（BOOKMAKER_DISABLE_SITES），测试完成后再放开",
        )

    await ensure_default_accounts(db, user_id)
    result = await db.execute(
        select(BookmakerAccount).where(
            and_(BookmakerAccount.user_id == user_id, BookmakerAccount.code == code)
        )
    )
    acc = result.scalar_one_or_none()
    if not acc:
        raise HTTPException(status_code=404, detail="站点配置不存在")

    password = decrypt_secret(acc.password_encrypted)
    session_token = decrypt_secret(acc.session_token_encrypted)
    base_url = acc.base_url or ""
    username = acc.username or ""
    old_balance = acc.balance if acc.balance and acc.balance > 0 else None
    old_profile = acc.profile_json if isinstance(acc.profile_json, dict) else {}

    # 释放读事务：verify 可能耗时 120s，持事务会触发 postgres idle_in_transaction 超时杀连接
    await db.commit()

    from app.services.bookmakers.plugins.ob.kaiyun import is_demo_url as _is_demo
    from app.services.bookmakers.site_profiles import is_live_site_code, needs_manual_venue
    from app.services.live_mode import reject_demo_url

    reject_demo_url(base_url)

    if needs_manual_venue(code):
        manual_venue = True

    if is_live_site_code(code) and not _is_demo(base_url):
        import httpx
        from app.services.bookmakers.gate_client import _gate_headers
        from app.services.bookmakers.gate_client import gate_url as _gate_url
        gate = _gate_url() or "http://host.docker.internal:9277"
        try:
            async with httpx.AsyncClient(timeout=3.0, headers=_gate_headers()) as client:
                try:
                    await client.post(
                        f"{gate}/prefer-login",
                        json={
                            "seconds": 100 if manual_venue else 50,
                            "base_url": base_url,
                            "site_code": code,
                        },
                    )
                except Exception:
                    pass
                h = await client.get(f"{gate}/health")
                if h.status_code != 200:
                    raise HTTPException(
                        status_code=503,
                        detail="无法连接本机浏览器网关。请运行: bash scripts/ensure_browser_gate.sh start",
                    )
        except HTTPException:
            raise
        except Exception:
            if not session_token:
                raise HTTPException(
                    status_code=503,
                    detail="无法连接本机浏览器网关。请运行: bash scripts/ensure_browser_gate.sh start",
                )

    from app.services.bookmakers.live_poller import pause_live_poller, resume_live_poller

    connector = get_connector(
        code,
        base_url=base_url,
        username=username,
        password=password,
        balance=old_balance,
        session_token=session_token,
        profile=old_profile,
        manual_venue=bool(manual_venue),
    )
    pause_live_poller()
    verify_timeout = 150.0 if manual_venue else 120.0
    timeout_msg = ""
    try:
        vr = await asyncio.wait_for(connector.verify(), timeout=verify_timeout)
    except asyncio.TimeoutError:
        timeout_msg = (
            "验证超时（150s）。请在弹出浏览器中手动进入指定场馆/盘口后再试"
            if manual_venue
            else "验证超时（120s）。自动进馆失败时可设 BOOKMAKER_MANUAL_VENUE=1"
        )
        vr = None
    finally:
        resume_live_poller()

    # 用新会话写结果，避免长耗时导致的连接失效
    from app.database import AsyncSessionLocal
    async with AsyncSessionLocal() as wdb:
        wres = await wdb.execute(
            select(BookmakerAccount).where(
                and_(BookmakerAccount.user_id == user_id, BookmakerAccount.code == code)
            )
        )
        acc2 = wres.scalar_one_or_none()
        if acc2:
            if vr is None or not vr.ok:
                # 超时但 Gate 仍有会话：保持 CONNECTED，避免滚球同步被跳过
                gate_alive = False
                try:
                    from app.services.bookmakers.gate_client import fetch_health

                    health = await fetch_health(timeout=3.0)
                    for item in (health or {}).get("session_balances") or []:
                        if str(item.get("site_code") or "").lower() == code:
                            gate_alive = True
                            bal = float(item.get("balance") or 0)
                            if bal > 0:
                                acc2.balance = Decimal(str(bal))
                            break
                except Exception:
                    gate_alive = False
                if gate_alive and vr is None:
                    acc2.status = BookmakerStatus.CONNECTED
                    acc2.last_error = timeout_msg
                    acc2.last_sync_at = datetime.now(timezone.utc)
                else:
                    acc2.status = BookmakerStatus.ERROR
                    acc2.last_error = timeout_msg or " ".join(
                        str((vr.message if vr else "验证失败") or "验证失败").split()
                    )
            else:
                acc2.status = BookmakerStatus.CONNECTED
                new_bal = Decimal(vr.balance or 0)
                if new_bal <= 0 and isinstance(vr.profile, dict) and vr.profile.get("balance") is not None:
                    try:
                        new_bal = Decimal(str(vr.profile.get("balance")))
                    except Exception:
                        new_bal = Decimal("0")
                confirmed_empty = False
                if isinstance(vr.profile, dict):
                    confirmed_empty = bool(vr.profile.get("venue_balance_confirmed")) and new_bal <= 0
                if new_bal > 0:
                    acc2.balance = new_bal
                elif confirmed_empty or (
                    isinstance(vr.profile, dict)
                    and str(vr.profile.get("balance_source") or "") in ("venue", "venue_empty")
                ):
                    # OB 侧栏 Hi 下方 0.00：覆盖错误的中心钱包缓存
                    acc2.balance = Decimal("0")
                acc2.last_error = None
                acc2.last_sync_at = datetime.now(timezone.utc)
                if vr.session_token:
                    acc2.session_token_encrypted = encrypt_secret(vr.session_token)
                if vr.profile:
                    merged = dict(acc2.profile_json) if isinstance(acc2.profile_json, dict) else {}
                    merged.update(vr.profile if isinstance(vr.profile, dict) else {})
                    acc2.profile_json = merged
            await wdb.commit()
            await wdb.refresh(acc2)
            serialized = _serialize_account(acc2)

    if vr is None:
        raise HTTPException(status_code=504, detail=timeout_msg)
    if not vr.ok:
        raise HTTPException(
            status_code=400,
            detail=" ".join(str(vr.message or "验证失败").split()),
        )

    return {
        "message": vr.message,
        "account": serialized,
        "profile": vr.profile,
    }


@router.post("/api/v1/bookmakers/verify-batch", response_model=APIResponse)
async def verify_bookmakers_batch(
    req: BookmakerVerifyBatchRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """并行验证多个站点（各站独立浏览器通道，互不等待）。"""
    await ensure_default_accounts(db, user.id)
    result = await db.execute(
        select(BookmakerAccount).where(BookmakerAccount.user_id == user.id)
    )
    by_code = {a.code: a for a in result.scalars().all()}

    if req.codes:
        codes = [c for c in req.codes if c in BOOKMAKER_CATALOG]
    else:
        codes = [
            a.code
            for a in by_code.values()
            if (a.base_url or "").strip() and (a.username or "").strip()
        ]
        order = list(BOOKMAKER_CATALOG.keys())
        codes.sort(key=lambda c: order.index(c) if c in order else 99)

    if not codes:
        raise HTTPException(status_code=400, detail="没有可验证的站点（请先填写网址与账号）")

    async def _one(code: str) -> dict:
        async with AsyncSessionLocal() as session:
            try:
                data = await _verify_bookmaker_account(
                    session, user.id, code, manual_venue=req.manual_venue
                )
                try:
                    await session.commit()
                except Exception:
                    await session.rollback()
                return {
                    "code": code,
                    "ok": True,
                    "message": data.get("message") or "验证成功",
                    "account": data.get("account"),
                }
            except HTTPException as e:
                try:
                    await session.rollback()
                except Exception:
                    pass
                detail = e.detail
                if not isinstance(detail, str):
                    detail = str(detail)
                return {"code": code, "ok": False, "message": detail, "account": None}
            except Exception as e:
                try:
                    await session.rollback()
                except Exception:
                    pass
                logger.exception("verify-batch failed for %s", code)
                return {"code": code, "ok": False, "message": str(e), "account": None}

    results = await asyncio.gather(*[_one(c) for c in codes])
    ok_n = sum(1 for r in results if r.get("ok"))
    return APIResponse(
        success=ok_n > 0,
        message=f"并行验证完成：成功 {ok_n}/{len(results)}",
        data={"results": list(results), "ok": ok_n, "total": len(results)},
    )


@router.post("/api/v1/bookmakers/{code}/verify", response_model=APIResponse)
async def verify_bookmaker(
    code: str,
    manual_venue: bool = False,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    data = await _verify_bookmaker_account(db, user.id, code, manual_venue=manual_venue)
    return APIResponse(
        message=data["message"],
        data={
            "account": data["account"],
            "profile": data["profile"],
        },
    )


@router.post("/api/v1/bookmakers/internal/browser-closed", response_model=APIResponse)
async def browser_closed_internal(
    payload: dict,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Gate 回调：用户关闭 Chromium 后断开该站全部后端连接。"""
    import os

    expected = (os.getenv("INTERNAL_API_TOKEN") or "").strip()
    got = (request.headers.get("X-Internal-Token") or "").strip()
    if not expected or got != expected:
        raise HTTPException(status_code=403, detail="forbidden")

    code = str((payload or {}).get("site_code") or "").lower().strip()
    base_url = str((payload or {}).get("base_url") or "").strip()
    if code not in BOOKMAKER_CATALOG:
        raise HTTPException(status_code=404, detail="未知站点")

    q = select(BookmakerAccount).where(BookmakerAccount.code == code)
    if base_url:
        q = q.where(BookmakerAccount.base_url == base_url)
    result = await db.execute(q)
    rows = list(result.scalars().all())
    n = 0
    for acc in rows:
        acc.session_token_encrypted = ""
        acc.status = BookmakerStatus.DISCONNECTED
        acc.balance = Decimal("0")
        acc.last_error = "浏览器已关闭，连接已断开"
        n += 1
    await db.flush()
    return APIResponse(message=f"已断开 {n} 个账户连接", data={"site_code": code, "count": n})


@router.post("/api/v1/bookmakers/{code}/disconnect", response_model=APIResponse)
async def disconnect_bookmaker(
    code: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """断开站点会话：清 Token、标未连接，并关闭本机 Browser Gate 长连接。"""
    if code not in BOOKMAKER_CATALOG:
        raise HTTPException(status_code=404, detail="未知站点")

    await ensure_default_accounts(db, user.id)
    result = await db.execute(
        select(BookmakerAccount).where(
            and_(BookmakerAccount.user_id == user.id, BookmakerAccount.code == code)
        )
    )
    acc = result.scalar_one_or_none()
    if not acc:
        raise HTTPException(status_code=404, detail="站点配置不存在")

    gate_msg = ""
    from app.services.bookmakers.gate_client import gate_url as _gate_url
    from app.services.bookmakers.plugins.ob.kaiyun import is_demo_url as _is_demo
    from app.services.bookmakers.site_profiles import is_live_site_code

    if is_live_site_code(code) and not _is_demo(acc.base_url or ""):
        import httpx

        from app.services.bookmakers.gate_client import _gate_headers

        gate = _gate_url() or "http://host.docker.internal:9277"
        try:
            async with httpx.AsyncClient(timeout=8.0, headers=_gate_headers()) as client:
                resp = await client.post(
                    f"{gate}/session/close",
                    json={"base_url": acc.base_url or "", "site_code": code},
                )
                data = resp.json() if resp.status_code < 500 else {}
                gate_msg = str(data.get("message") or "")
        except Exception as e:
            gate_msg = f"网关未响应（会话已在服务端清除）: {e}"

    acc.session_token_encrypted = ""
    acc.status = BookmakerStatus.DISCONNECTED
    acc.last_error = None
    # 断开后不再展示网站余额（避免陈旧缓存被当成真实资产）
    acc.balance = Decimal("0")
    await db.flush()

    msg = "已断开连接"
    if gate_msg:
        msg = f"{msg}；{gate_msg}"
    return APIResponse(
        message=msg,
        data={"account": _serialize_account(acc)},
    )


@router.post("/api/v1/bookmakers/{code}/purge-resync", response_model=APIResponse)
async def purge_and_resync_bookmaker(
    code: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """删除指定站点脏赛事/赔率后重新同步写入。"""
    if code not in BOOKMAKER_CATALOG:
        raise HTTPException(status_code=404, detail="未知站点")
    from app.services.bookmakers.purge import (
        clear_ai_recs_cache,
        purge_bookmaker_matches,
    )

    purged = await purge_bookmaker_matches(db, code)
    await db.commit()
    cache_n = 0
    try:
        cache_n = await clear_ai_recs_cache(site_code=code)
    except Exception:
        cache_n = 0
    # 重新拉取全部已连接站点（含目标站）；目标站未连接时仅完成清理
    result = await sync_user_bookmakers(db, user.id)
    return APIResponse(
        message=f"{BOOKMAKER_CATALOG[code].get('name') or code} 已清库并重新同步",
        data={"purged": purged, "cache_cleared": cache_n, "sync": result},
    )


@router.post("/api/v1/bookmakers/sync", response_model=APIResponse)
async def sync_bookmakers(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    result = await sync_user_bookmakers(db, user.id)
    return APIResponse(message="滚球足球/篮球同步完成", data=result)


@router.post("/api/v1/bookmakers/sync-live", response_model=APIResponse)
async def sync_live_bookmakers(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """轻量滚球同步：仅足球/篮球比分/时钟/赔率。"""
    result = await sync_live_scores_odds(db, user.id)
    return APIResponse(message="滚球同步完成", data=result)


@router.get("/api/v1/bookmakers/odds-compare/{match_id}", response_model=APIResponse)
async def odds_compare(
    match_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    data = await compare_match_odds(db, match_id)
    return APIResponse(data=data)
