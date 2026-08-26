"""
统一下单执行器 — 自动投注 / 手动投注共用同一套完整逻辑。

包含：
  1. 跨站比价选最优站点（best_by_selection）
  2. provider_code 四级 fallback 解析
  3. 未连接站点自动切换
  4. 动态仓位（按站点 ROI / 连败独立核算）
  5. 下单重试机制（每次重试取最新赔率）
  6. Bet / Transaction 落库 + WebSocket 通知
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func, select

from app.models.user import (
    User, Match, Bet, BetStatus, BetType, Transaction, TransactionType,
    BookmakerAccount, BookmakerStatus, Odds,
)
from app.ai.strategy import BetDecision, StrategyConfig
from app.core.websocket import manager
from app.core.crypto import decrypt_secret
from app.config import settings

logger = logging.getLogger(__name__)

# 单边模式可下注站点
SINGLE_SIDE_PROVIDER_NAMES = frozenset({"平博", "OB体育"})
SINGLE_SIDE_PROVIDER_CODES = frozenset({"pinnacle", "ob"})


def _place_failure_is_retryable(message: str) -> bool:
    """Only retry failures known to happen before an order can be submitted.

    DOM selection/safety failures already run their own targeted recovery.  Retrying
    them blindly repeats the same click path; retrying an ambiguous post-click failure
    can even create a duplicate order.
    """
    text = str(message or "").lower()
    transient_markers = (
        "浏览器网关正忙",
        "本站浏览器正忙",
        "browser gate",
        "connection refused",
        "connect timeout",
        "read timeout",
        "timed out",
        "timeout",
        "超时",
        "temporarily unavailable",
        "service unavailable",
    )
    return any(marker.lower() in text for marker in transient_markers)


def _provider_row_matches(row: Optional[Odds], provider_code: str, provider_label: str) -> bool:
    """判断赔率行是否确实属于目标站点（允许历史 code/name 两种写法）。"""
    if row is None:
        return False
    aliases = {
        "pinnacle": {"平博", "pinnacle"},
        "ob": {"ob体育", "ob sports", "ob"},
    }.get(str(provider_code or "").lower(), {str(provider_label or "").strip().lower()})
    return str(getattr(row, "provider", "") or "").strip().lower() in aliases


@dataclass
class BetExecResult:
    """下单执行结果。"""
    ok: bool = False
    bet_id: int | None = None
    provider_code: str = ""
    provider_label: str = ""
    stake: Decimal = Decimal("0")
    odds: float = 0.0
    line: float | None = None
    external_bet_id: str | None = None
    potential_payout: Decimal = Decimal("0")
    message: str = ""
    site_balance: float = 0.0


# ── 独立工具函数（从 AutoBetter 提取，不依赖 self）──────────────────

async def get_best_market_pack(
    db: AsyncSession,
    match_id: int,
    bet_type: str = "total",
    *,
    providers_filter: set[str] | None = None,
) -> dict:
    """精选全场大小球盘口，跨站比价；其它玩法直接返回空。"""
    from app.ai.market_recommend import load_market_matrix
    from app.services.provider_utils import (
        best_by_selection as _best_by_selection,
        code_by_provider as _code_by_provider,
    )

    bt = str(bet_type or "total").lower()
    if bt != "total":
        return {
            "odds": {},
            "best_by_selection": {},
            "odds_by_provider": {},
            "line": None,
            "bet_type": bt,
        }
    matrix, _spread_line, total_line = await load_market_matrix(
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
    return {
        "odds": odds,
        "best_by_selection": best_by_selection,
        "odds_by_provider": odds_by_provider,
        "line": line,
        "bet_type": bt,
    }


async def get_odds_row(
    db: AsyncSession,
    match_id: int,
    *,
    provider_name_prefer: str = "平博",
    bet_type: BetType = BetType.TOTAL,
) -> Optional[Odds]:
    """只返回指定站点、指定盘口类型的当前赔率行。

    绝不能在 total 行缺失时回退到 spread/moneyline：那会把同名
    ``under/over`` 字段误当成大小球，实际下成让球单。
    """
    match_ids = [int(match_id)]
    try:
        from app.models.user import Match as _Match
        from app.services.fixture_key import sibling_match_ids

        m = await db.get(_Match, int(match_id))
        if m is not None:
            for sid in await sibling_match_ids(db, m):
                if sid and int(sid) not in match_ids:
                    match_ids.append(int(sid))
    except Exception:
        pass

    # 自动投注只消费实时盘口。valid_to=NULL 并不代表仍然在场：当 Gate
    # 暂时返回空列表时，旧版本可能继续保持 current。超过 5 分钟直接失效，
    # 避免对已结束/已下架赛事重复执行 UI 点击。
    fresh_after = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5)

    result = await db.execute(
        select(Odds).where(
            Odds.match_id.in_(match_ids),
            Odds.bet_type == bet_type,
            Odds.provider == provider_name_prefer,
            Odds.valid_to.is_(None),
            Odds.is_live.is_(True),
            func.coalesce(Odds.last_seen_at, Odds.valid_from) >= fresh_after,
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
            Odds.is_live.is_(True),
            func.coalesce(Odds.last_seen_at, Odds.valid_from) >= fresh_after,
        )
    )
    odds_list = list(result.scalars().all())
    # provider 别名只在同一站点内归一化，禁止跨站或跨玩法接管。
    preferred = str(provider_name_prefer or "").strip().lower()
    aliases = {
        "平博": {"平博", "pinnacle"},
        "pinnacle": {"平博", "pinnacle"},
        "ob体育": {"ob体育", "ob sports", "ob"},
        "ob": {"ob体育", "ob sports", "ob"},
    }.get(preferred, {preferred})
    for o in odds_list:
        if str(o.provider or "").strip().lower() in aliases:
            return o
    return None


async def mark_bet_pending(
    user_id: int,
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
            f"ai:bet:pending:{user_id}:{match_id}",
            payload,
            ttl=21600,
        )
        logger.info("已记录待定注单 match=%s orderNo=%s（防重复 6h）", match_id, order_no)
    except Exception as e:
        logger.warning("记录待定注单失败: %s", e)


async def _notify(user_id: int, event_type: str, data: dict):
    """推送 WebSocket 通知。"""
    await manager.broadcast_to_user(user_id, {
        "type": f"ai_{event_type}",
        "data": data,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


# ── 核心下单执行 ──────────────────────────────────────────────────

async def execute_bet(
    db: AsyncSession,
    user: User,
    match: Match,
    decision: BetDecision,
    strat_cfg: StrategyConfig,
    *,
    is_auto: bool = False,
) -> BetExecResult:
    """
    统一下单执行（自动/手动共用）。

    自动投注: is_auto=True，调用方先完成引擎锁/模式检查/同场防重等前置过滤。
    手动投注: is_auto=False，调用方先完成推荐获取/策略闸门校验。

    本函数负责：
      跨站比价 → provider 解析 → 未连接切站 → 赔率/线解析 → 仓位校验
      → connector 下单（含重试）→ Bet/Transaction 落库 → 通知
    """
    from app.services.bookmakers.catalog import provider_name
    from app.services.bookmakers.registry import is_real_live_account, get_connector
    from app.services.provider_utils import (
        site_code_from_match,
        code_by_provider as _code_by_provider,
    )
    from app.services.bookmakers.plugins.ob.odds import is_virtual_match
    from app.services.bookmakers.china_match import is_china_match
    from app.core.cache import cache

    sel = str(decision.selection or "").lower()
    bet_type = str(getattr(decision, "bet_type", None) or "total").lower()

    # ── 玩法白名单 ──
    ALLOWED_BET_TYPES = {"total"}
    allowed_sels = {"under", "over"}
    if bet_type not in ALLOWED_BET_TYPES or sel not in allowed_sels:
        logger.warning(
            "[下单] ❌ 不支持的盘口/方向: match=%s type=%s sel=%s",
            decision.match_id, bet_type, sel,
        )
        return BetExecResult(ok=False, message=f"不支持的盘口/方向: {bet_type}/{sel}")
    if sel == "over" and not bool(getattr(settings, "AI_ENABLE_OVER", True)):
        logger.info("[下单] over 已由 AI_ENABLE_OVER 关闭: match=%s", decision.match_id)
        return BetExecResult(ok=False, message="不支持大球投注（AI_ENABLE_OVER=false）")

    sport_key = match.sport.value if hasattr(match.sport, "value") else str(match.sport)
    if is_virtual_match(sport_key, match.league or "", match.home_team or "", match.away_team or ""):
        return BetExecResult(ok=False, message="虚拟赛事不支持下单")
    if is_china_match(match.league or "", match.home_team or "", match.away_team or "", sport_key):
        return BetExecResult(ok=False, message="中国赛事不支持下单")

    logger.info(
        "[下单] 准备下单 match=%s %s vs %s | sel=%s conf=%.2f odds=%.2f stake=%.2f | provider=%s | auto=%s",
        decision.match_id, match.home_team or "?", match.away_team or "?",
        sel, float(decision.confidence or 0), float(decision.odds or 0),
        float(decision.suggested_stake or 0), str(decision.provider_code or "?"), is_auto,
    )

    # ── 1. 跨站比价 ──
    pack = await get_best_market_pack(
        db,
        decision.match_id,
        bet_type,
        providers_filter=SINGLE_SIDE_PROVIDER_NAMES,
    )
    # 下单前再次确认数据库里确实有当前全场大小球的同向赔率和盘口线。
    # 没有 total 行时禁止使用 decision.odds 继续点 UI，避免把旧让球赔率
    # 当成 under/over。
    pack_odds = pack.get("odds") or {}
    if sel not in pack_odds or not pack.get("line"):
        msg = "未找到当前全场大小球赔率，已阻止下单"
        logger.warning(
            "[下单] ❌ total 赔率行缺失: match=%s sel=%s odds_keys=%s line=%s",
            decision.match_id, sel, sorted(pack_odds), pack.get("line"),
        )
        return BetExecResult(ok=False, message=msg)
    best_meta = (pack.get("best_by_selection") or {}).get(sel) or {}

    # ── 2. provider_code 四级 fallback ──
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

    # ── 3. 检查站点连接状态，未连接则自动切换 ──
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
        odds_by_provider = pack.get("odds_by_provider") or {}
        switched = False
        for pname, sel_odds in odds_by_provider.items():
            alt_code = _code_by_provider(pname) or ""
            if alt_code not in connected_codes or alt_code == provider_code:
                continue
            if sel not in (sel_odds or {}):
                continue
            logger.info(
                "[下单] 站点切换: %s未连接 -> 切换至%s | match=%s sel=%s odds=%.2f",
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
            msg = f"请先连接{provider_label}后再下单"
            logger.warning("[下单] 站点未连接且无可用替代 site=%s connected=%s", provider_code, connected_codes or "无")
            await _notify(user.id, "bet_failed", {
                "match_id": decision.match_id,
                "message": msg,
            })
            return BetExecResult(ok=False, message=msg, provider_code=provider_code, provider_label=provider_label)

    # ── 4. 赛事 ID 解析（支持跨站队名匹配） ──
    ids = dict((match.extra_data or {}).get("ids") or {})
    match_ext = str(ids.get(provider_code) or "")
    if not match_ext and str(match.external_id or "").startswith(f"{provider_code}:"):
        match_ext = str(match.external_id)
    sib_match = None  # 目标站点的同场赛事（用于获取赔率）
    if not match_ext:
        # 通过 sibling_match_ids 查找目标站点的同场赛事 ID
        try:
            from app.services.fixture_key import sibling_match_ids as _sib
            sib_ids = await _sib(db, match)
            if sib_ids:
                sib_res = await db.execute(
                    select(Match).where(
                        Match.id.in_(sib_ids),
                        Match.external_id.like(f"{provider_code}:%"),
                    ).limit(1)
                )
                sib_match = sib_res.scalar_one_or_none()
                if sib_match:
                    sib_extra = dict((sib_match.extra_data or {}).get("ids") or {})
                    match_ext = str(sib_extra.get(provider_code) or sib_match.external_id or "")
                    logger.info("[下单] 通过 sibling 找到 %s 赛事 ID: match=%s -> %s", provider_code, decision.match_id, match_ext)
        except Exception as e:
            logger.debug("[下单] sibling 查找赛事 ID 失败: %s", e)
    if not match_ext:
        # 跨站队名匹配：用主客队名构造合成 external_id
        # 平博格式 pinnacle:home|away → site_bet.py 解析队名后在 DOM 上搜索
        # OB 格式 ob:home|away → OB 下单靠 odds_data._ob 结构化参数，队名仅用于页面搜索
        home_team = str(match.home_team or "")
        away_team = str(match.away_team or "")
        if home_team and away_team:
            match_ext = f"{provider_code}:{home_team}|{away_team}"
            logger.info("[下单] 构造合成赛事 ID: match=%s -> %s (队名匹配模式)", decision.match_id, match_ext)
    if not match_ext:
        msg = "缺少对应站点赛事 ID，请先同步该站滚球"
        logger.warning("[下单] 缺少赛事 ID: match=%s site=%s", decision.match_id, provider_code)
        await _notify(user.id, "bet_failed", {
            "match_id": decision.match_id,
            "message": msg,
        })
        return BetExecResult(ok=False, message=msg, provider_code=provider_code, provider_label=provider_label)

    # ── 5. 赔率行 + line 解析 ──
    bt_enum = BetType.TOTAL
    # 切换站点时优先从 sibling 获取目标站点赔率
    odds_match_id = decision.match_id
    if sib_match and sib_match.id != decision.match_id:
        odds_match_id = sib_match.id
        logger.info("[下单] 从 sibling 获取赔率: match=%s -> sib=%s", decision.match_id, sib_match.id)
    odds_row = await get_odds_row(
        db,
        odds_match_id,
        provider_name_prefer=provider_label,
        bet_type=bt_enum,
    )
    # sibling 没有赔率时回退到原 match
    if not odds_row and odds_match_id != decision.match_id:
        odds_row = await get_odds_row(
            db,
            decision.match_id,
            provider_name_prefer=provider_label,
            bet_type=bt_enum,
        )
    odds_payload = dict(odds_row.odds_data or {}) if odds_row else {}
    line_val = None
    row_is_own_site = _provider_row_matches(odds_row, provider_code, provider_label)
    if bet_type == "total":
        if row_is_own_site and odds_row.total is not None:
            try:
                line_val = float(odds_row.total)
            except (TypeError, ValueError):
                line_val = None
        if line_val is None and row_is_own_site:
            try:
                line_val = float(odds_payload.get("line") or odds_payload.get("total") or 0)
            except (TypeError, ValueError):
                line_val = None
    # 目标站点没有自己的 total 行/方向赔率/盘口线时，禁止用决策缓存或另一站
    # 的 pack 数据继续点 UI；这正是让球单混入平博注单的主要风险路径。
    try:
        row_side_odds = float(odds_payload.get(sel) or 0)
    except (TypeError, ValueError):
        row_side_odds = 0.0
    if not row_is_own_site or row_side_odds <= 1.0 or line_val is None or line_val <= 0:
        msg = "目标站点缺少当前全场大小球赔率/盘口线，已阻止下单"
        logger.warning(
            "[下单] ❌ 目标站点 total 行不完整: match=%s site=%s provider=%s sel=%s odds=%.3f line=%s",
            decision.match_id, provider_code, getattr(odds_row, "provider", None), sel, row_side_odds, line_val,
        )
        return BetExecResult(ok=False, message=msg, provider_code=provider_code, provider_label=provider_label)
    if line_val is not None and bet_type == "total":
        odds_payload = {**odds_payload, "line": line_val, "total": line_val}

    # 分析快照与实际下注快照必须是同一条全场线。哪怕只变动半球/四分之一，
    # 亚洲盘的结算区间也已改变，直接停止并等待下一轮重新分析。
    try:
        decision_line = float(decision.line or 0)
    except (TypeError, ValueError):
        decision_line = 0.0
    if decision_line > 0 and line_val is not None and abs(line_val - decision_line) > 0.01:
        msg = f"盘口已从分析线 {decision_line:g} 变为 {line_val:g}，已阻止使用旧分析下单"
        logger.warning(
            "[下单] ❌ 分析盘口过期 match=%s site=%s decision_line=%s current_line=%s",
            decision.match_id, provider_code, decision_line, line_val,
        )
        await _notify(user.id, "bet_failed", {"match_id": decision.match_id, "message": msg})
        return BetExecResult(ok=False, message=msg, provider_code=provider_code, provider_label=provider_label)

    # ── 6. 当前赔率（优先数据库最新）──
    try:
        current_odds = float(odds_payload.get(sel) or 0)
    except (TypeError, ValueError):
        current_odds = 0.0
    if current_odds <= 0:
        logger.warning("[下单] ❌ 赔率无效 match=%s type=%s sel=%s odds=0", decision.match_id, bet_type, sel)
        return BetExecResult(ok=False, message="赔率无效", provider_code=provider_code, provider_label=provider_label)

    # 赔率大幅跳变时不能把旧模型置信度直接套到新价位；小幅跳水交给站点
    # 原生接受更优赔率策略处理，大幅变化则重新分析。
    decision_odds = float(decision.odds or 0)
    if decision_odds > 1.0 and abs(current_odds - decision_odds) > 0.10:
        msg = f"赔率已从分析值 {decision_odds:.3f} 变为 {current_odds:.3f}，已阻止使用旧分析下单"
        logger.warning(
            "[下单] ❌ 分析赔率过期 match=%s site=%s decision_odds=%.3f current_odds=%.3f",
            decision.match_id, provider_code, decision_odds, current_odds,
        )
        await _notify(user.id, "bet_failed", {"match_id": decision.match_id, "message": msg})
        return BetExecResult(ok=False, message=msg, provider_code=provider_code, provider_label=provider_label)

    # 赔率变动记录
    if decision_odds > 0 and abs(current_odds - decision_odds) > 0.05:
        if current_odds > decision_odds + 0.05:
            logger.info("[下单] 赔率上调 match=%s 决策=%.2f 最新=%.2f", decision.match_id, decision_odds, current_odds)
        else:
            logger.info("[下单] 赔率变动 match=%s 决策=%.2f 最新=%.2f", decision.match_id, decision_odds, current_odds)

    # ── 7. 站点账户 + 仓位校验 ──
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
        msg = f"请先连接{provider_label}后再下单"
        logger.warning("[下单] 站点未连接 site=%s", provider_code)
        await _notify(user.id, "bet_failed", {
            "match_id": decision.match_id,
            "message": msg,
        })
        return BetExecResult(ok=False, message=msg, provider_code=provider_code, provider_label=provider_label)

    stake = Decimal(str(decision.suggested_stake or 0)).quantize(Decimal("0.01"))
    if stake < Decimal("1.00"):
        logger.warning("[下单] ❌ 仓位异常 match=%s suggested_stake=%s", decision.match_id, decision.suggested_stake)
        return BetExecResult(ok=False, message="仓位异常", provider_code=provider_code, provider_label=provider_label)
    if float(site_acc.balance or 0) < float(stake):
        # 余额不足时尝试切换到另一个已连接且有余额的站点
        try:
            alt_res = await db.execute(
                select(BookmakerAccount).where(
                    BookmakerAccount.user_id == user.id,
                    BookmakerAccount.enabled.is_(True),
                    BookmakerAccount.status == BookmakerStatus.CONNECTED,
                    BookmakerAccount.code != provider_code,
                    BookmakerAccount.code.in_(list(SINGLE_SIDE_PROVIDER_CODES)),
                )
            )
            alt_accounts = alt_res.scalars().all()
        except Exception as e:
            logger.warning("[下单] 余额不足时读取替代站点失败: %s", e)
            alt_accounts = []
        switched = False
        for alt_acc in alt_accounts:
            logger.info(
                "[下单] 余额切换检查: alt=%s real=%s balance=%.2f stake=%.2f",
                alt_acc.code,
                is_real_live_account(alt_acc.code, alt_acc.base_url or ""),
                float(alt_acc.balance or 0),
                float(stake),
            )
            if not is_real_live_account(alt_acc.code, alt_acc.base_url or ""):
                continue
            if float(alt_acc.balance or 0) < float(stake):
                continue
            # 切换到有余额的替代站点
            alt_code = alt_acc.code
            # 重新解析赛事 ID：先从当前 match 的 ids 取，再通过 sibling 查找平博同场赛事
            alt_match_ext = str(ids.get(alt_code) or "")
            if not alt_match_ext and str(match.external_id or "").startswith(f"{alt_code}:"):
                alt_match_ext = str(match.external_id)
            if not alt_match_ext:
                # 通过 sibling_match_ids 查找平博同场赛事 ID
                try:
                    from app.services.fixture_key import sibling_match_ids
                    sib_ids = await sibling_match_ids(db, match)
                    if sib_ids:
                        sib_res = await db.execute(
                            select(Match).where(
                                Match.id.in_(sib_ids),
                                Match.external_id.like(f"{alt_code}:%"),
                            ).limit(1)
                        )
                        sib_match = sib_res.scalar_one_or_none()
                        if sib_match:
                            sib_extra = dict((sib_match.extra_data or {}).get("ids") or {})
                            alt_match_ext = str(sib_extra.get(alt_code) or sib_match.external_id or "")
                            if alt_match_ext:
                                logger.info(
                                    "[下单] 余额切换: 通过 sibling 找到 %s 赛事ID sib_match=%s ext=%s",
                                    alt_code, sib_match.id, alt_match_ext,
                                )
                except Exception as e:
                    logger.warning("[下单] 余额切换 sibling 查找失败: %s", e)
            if not alt_match_ext:
                logger.info("[下单] 余额切换: %s 缺少赛事ID ids=%s ext=%s", alt_code, ids, str(match.external_id or ""))
                continue
            alt_odds_row = await get_odds_row(
                db,
                decision.match_id,
                provider_name_prefer=provider_name(alt_code),
                bet_type=bt_enum,
            )
            if not _provider_row_matches(alt_odds_row, alt_code, provider_name(alt_code)):
                logger.info("[下单] 余额切换: %s 缺少本站全场大小球行，跳过", alt_code)
                continue
            alt_odds_payload = dict(alt_odds_row.odds_data or {}) if alt_odds_row else {}
            try:
                alt_current_odds = float(alt_odds_payload.get(sel) or 0)
                alt_line = float(alt_odds_row.total or alt_odds_payload.get("line") or alt_odds_payload.get("total") or 0)
            except (TypeError, ValueError):
                alt_current_odds = 0.0
                alt_line = 0.0
            if alt_current_odds <= 1.0 or alt_line <= 0:
                continue
            logger.info(
                "[下单] 余额不足切换: %s(%.2f) -> %s(%.2f) | match=%s sel=%s odds=%.2f",
                provider_label, float(site_acc.balance or 0),
                provider_name(alt_code), float(alt_acc.balance or 0),
                decision.match_id, sel, alt_current_odds,
            )
            provider_code = alt_code
            provider_label = provider_name(provider_code)
            site_acc = alt_acc
            match_ext = alt_match_ext
            odds_row = alt_odds_row
            odds_payload = alt_odds_payload
            current_odds = alt_current_odds
            # 重新解析本站 total line
            line_val = alt_line
            switched = True
            break
        if not switched:
            msg = f"{provider_label}余额不足（当前 {float(site_acc.balance or 0):.2f}），无可用替代站点"
            logger.warning("[下单] %s", msg)
            await _notify(user.id, "bet_failed", {
                "match_id": decision.match_id,
                "message": msg,
            })
            return BetExecResult(ok=False, message=msg, provider_code=provider_code, provider_label=provider_label)

    # ── 8. connector 创建 ──
    connector = get_connector(
        provider_code,
        base_url=site_acc.base_url,
        username=site_acc.username,
        password=decrypt_secret(site_acc.password_encrypted),
        balance=site_acc.balance,
        session_token=decrypt_secret(site_acc.session_token_encrypted),
        profile=site_acc.profile_json if isinstance(site_acc.profile_json, dict) else {},
    )

    # ── 9. 暂停滚球轮询 + 下单重试 ──
    # 释放 DB 事务：Gate 下单可能 60–120s，持事务会触发 postgres idle_in_transaction 超时杀连接。
    # expire_on_commit=False 保证 ORM 对象在 commit 后仍可访问。
    try:
        if db.in_transaction():
            await db.commit()
    except Exception:
        try:
            await db.rollback()
        except Exception:
            pass

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
    attempts_used = 0
    for attempt in range(1 + retry_count):
        if attempt > 0:
            logger.info("[下单] 补单重试 %s/%s: match=%s 等待 %.1fs", attempt, retry_count, decision.match_id, retry_delay)
            await asyncio.sleep(retry_delay)
            # 重新获取最新赔率
            fresh_odds_row = await get_odds_row(
                db,
                decision.match_id,
                provider_name_prefer=provider_label,
                bet_type=bt_enum,
            )
            if fresh_odds_row:
                fresh_payload = dict(fresh_odds_row.odds_data or {})
                if bet_type == "total":
                    try:
                        fresh_line = float(
                            fresh_odds_row.total
                            or ((fresh_payload.get("_site") or {}).get("line"))
                            or fresh_payload.get("line")
                            or fresh_payload.get("total")
                            or 0
                        )
                    except (AttributeError, TypeError, ValueError):
                        fresh_line = 0.0
                    if fresh_line > 0:
                        line_val = fresh_line
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
            logger.info("[下单] 补单重试 %s/%s: match=%s 最新赔率=%.3f", attempt, retry_count, decision.match_id, current_odds)

        # 队名注入 odds_data
        odds_payload["_home_team"] = match.home_team or ""
        odds_payload["_away_team"] = match.away_team or ""
        odds_payload["_is_live"] = True
        odds_payload["_stake_policy"] = {
            "dynamic_stake": str(stake),
            "max_stake": str(strat_cfg.max_bet_amount or stake),
            "available_balance": str(site_acc.balance or 0),
        }

        place = await connector.place_bet(
            match_external_id=str(match_ext or match.external_id),
            selection=sel,
            odds=float(current_odds),
            stake=stake,
            bet_type=bet_type,
            odds_data=odds_payload,
        )
        attempts_used = attempt + 1
        if place.ok:
            _actual = getattr(place, "actual_stake", None)
            if _actual:
                actual_stake = Decimal(str(_actual))
                if actual_stake > 0:
                    stake = actual_stake.quantize(Decimal("0.01"))
            if provider_code == "ob" and place.external_bet_id:
                await mark_bet_pending(
                    user.id,
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
                logger.info("OB 下单回执已返回 orderNo=%s，按成功受理", place.external_bet_id)
        if place.ok:
            break
        logger.warning("[下单] 失败 (attempt %s/%s): %s", attempt + 1, 1 + retry_count, place.message)
        if attempt >= retry_count or not _place_failure_is_retryable(place.message):
            if attempt < retry_count:
                logger.info(
                    "[下单] 不重复执行非瞬时/可能已点击的失败: match=%s reason=%s",
                    decision.match_id,
                    str(place.message or "")[:240],
                )
            break

    if not place or not place.ok:
        msg = place.message if place else "unknown"
        logger.warning("[下单] 真实下单失败（尝试 %s 次）: %s", attempts_used, msg)
        await _notify(user.id, "bet_failed", {
            "match_id": decision.match_id,
            "message": msg or f"{provider_label}下单失败",
        })
        if _resume_fn:
            _resume_fn()
        return BetExecResult(ok=False, message=msg, provider_code=provider_code, provider_label=provider_label)

    # ── 10. 余额更新 ──
    try:
        bal = await connector.fetch_balance()
        site_acc.balance = bal
    except Exception:
        if place.balance_after and place.balance_after > 0:
            site_acc.balance = place.balance_after
        else:
            site_acc.balance = Decimal(str(site_acc.balance or 0)) - stake

    # ── 11. Bet + Transaction 落库 ──
    potential_payout = (stake * Decimal(str(current_odds))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    line_tag = f" {line_val}" if line_val is not None else ""
    bet = Bet(
        user_id=user.id,
        match_id=decision.match_id,
        bet_type=bet_type,
        selection=sel,
        odds=current_odds,
        stake=stake,
        potential_payout=potential_payout,
        actual_payout=Decimal("0"),
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

    # 提交事务：确保 Bet + Transaction 持久化，防止下一轮 _match_bet_count 查不到导致重复下单
    commit_ok = True
    try:
        await db.commit()
    except Exception as e:
        commit_ok = False
        logger.warning("[下单] Bet 落库 commit 失败: %s", e)
        try:
            await db.rollback()
        except Exception:
            pass
        # commit 失败时写 Redis 防止下一轮重复下单（覆盖所有 provider）
        try:
            await cache.set_json(
                f"ai:bet:pending:{user.id}:{decision.match_id}",
                {
                    "order_no": place.external_bet_id or "",
                    "time": datetime.now(timezone.utc).isoformat(),
                    "selection": sel,
                    "bet_type": bet_type,
                    "odds": current_odds,
                    "stake": float(stake),
                    "line": line_val,
                    "confidence": float(decision.confidence or 0),
                    "provider": provider_label,
                    "commit_failed": True,
                },
                ttl=21600,
            )
            logger.info("[下单] commit 失败已写 Redis 防重复 match=%s", decision.match_id)
        except Exception:
            pass

    logger.info(
        "[下单] ✅ 成功 match=%s | sel=%s line=%s stake=%.2f odds=%.2f conf=%.2f ext=%s | 预计赔付=%.2f | commit=%s",
        decision.match_id, sel, line_val, float(stake), current_odds,
        float(decision.confidence or 0), place.external_bet_id,
        float(potential_payout), "ok" if commit_ok else "FAIL",
    )

    await _notify(user.id, "bet_placed", {
        "bet_id": bet.id if commit_ok else None,
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

    # 清除 Redis 待定标记（仅在 commit 成功时清除）
    if commit_ok:
        try:
            await cache.delete(f"ai:bet:pending:{user.id}:{decision.match_id}")
        except Exception:
            pass

    if _resume_fn:
        _resume_fn()

    return BetExecResult(
        ok=True,
        bet_id=bet.id,
        provider_code=provider_code,
        provider_label=provider_label,
        stake=stake,
        odds=current_odds,
        line=line_val,
        external_bet_id=place.external_bet_id,
        potential_payout=potential_payout,
        site_balance=float(site_acc.balance or 0),
    )
