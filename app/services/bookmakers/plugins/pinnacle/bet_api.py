"""平博 API 下单器：页面上下文 fetch bet-placement/buyV4（替代 UI 点击）。

逆向自真实成交抓包（女王公园巡游者 4.25 让球盘，2026-08-15 22:44:27）：

  POST {origin}/bet-placement/buyV4?uniqueRequestId={uuid}&locale=zh_CN&withCredentials=true
  {
    "acceptBetterOdds": true,
    "oddsFormat": 1,
    "selections": [{
      "odds": "1.800",
      "oddsId": "1634016992|0|3|3|1|4.25",          # 赛事ID|periodFlag|市场3|period|方向|线
      "selectionId": "3692533121|0|1634016992|0|3|3|0|4.25|0",
      "stake": 10,
      "uniqueRequestId": "{uuid}",
      "wagerType": "NORMAL",
      "betLocationTracking": { ... 固定埋点 },
      "winRiskStake": "RISK"
    }]
  }

关键依赖：odds 采集时保留的 _site.selections.{side}.id（odds.py sel_id 字段）。
oddsId 由 mid + 盘口类型 + period + 方向 + 线拼装；selectionId 优先用采集到的
真实 id（tot_row[4]），缺失时回退构造。
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from decimal import Decimal
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 固定埋点（抓包原样，站点不校验具体值）
_BET_LOCATION_TRACKING = {
    "view": "NEW_ASIAN_VIEW",
    "navigation": "SPORTS",
    "device": "DESKTOP",
    "reuseSelection": False,
    "mainPages": "SPORT",
    "marketTab": "TODAY",
    "market": "MATCHES",
    "oddsContainerCategory": "MAIN",
    "oddsContainerTitle": "TODAY",
    "language": "zh_CN",
    "displayMode": "LIGHT",
    "marketType": "_3LINES",
    "eventSorting": "LEAGUE",
    "pageType": "DOUBLE",
    "timeZone": "Asia/Shanghai",
    "defaultPage": "TODAY",
    "isLiveStreamPlaying": None,
}

# oddsId 编码段：market 类型（3=让球/spread，同结构也用于大小）
_MARKET_SPREAD = "3"
_PERIOD_FULL = "3"  # 全场（period=0 表全场，第2段与第8段呼应）

# side → oddsId 方向段
# 抓包样本：女王公园 4.25(让球) dir=1；17:14 两单 0.5/2 线 dir=1。
# Pinnacle 紧凑格式该段为 selectionType（1=NORMAL），并非 over/under 方向——
# 方向由 oddsId 尾段线的正负（让球）或大小球的 over/under 在 selectionId 里区分。
# 大小球：over/under 共用同一 oddsId（同线同市场），方向在 selectionId 段7。
_SIDE_DIR = {"over": "1", "under": "1", "home": "1", "away": "2"}
_SEL_DIR = {"over": "1", "under": "2", "home": "1", "away": "2"}


def _build_ids(
    match_external_id: str,
    selection: str,
    line: Optional[float],
    odds_data: dict,
) -> tuple[str, str, str] | None:
    """构造 (oddsId, selectionId, periodFlag)。优先用采集的真实 sel_id。"""
    mid = ""
    ext = str(match_external_id or "")
    if ext.startswith("pinnacle:"):
        mid = ext.split(":", 1)[1]
    site = (odds_data or {}).get("_site") or {}
    if not mid:
        mid = str(site.get("mid") or "")
    if not mid:
        return None

    side = (selection or "").lower()
    sel_real = ""
    try:
        sel_real = str((((site.get("selections") or {}).get(side)) or {}).get("id") or "")
    except Exception:
        sel_real = ""

    line_v = line
    if line_v is None:
        try:
            line_v = float(site.get("line") or 0) or None
        except (TypeError, ValueError):
            line_v = None
    if line_v is None:
        return None
    line_s = f"{line_v:g}"

    bet_type = str(site.get("bet_type") or "total").lower()
    # 大小球与让球共用 market=3 结构；独赢不同（暂不支持）
    if bet_type not in ("total", "spread"):
        return None

    direction = _SIDE_DIR.get(side)
    sel_dir = _SEL_DIR.get(side)
    if not direction or not sel_dir:
        return None

    odds_id = f"{mid}|0|{_MARKET_SPREAD}|{_PERIOD_FULL}|{direction}|{line_s}"
    if sel_real:
        selection_id = sel_real
    else:
        # 9段格式：{裸id}|0|{mid}|0|3|3|{selDir}|{线2位小数}|1
        selection_id = f"0|0|{mid}|0|{_MARKET_SPREAD}|{_PERIOD_FULL}|{sel_dir}|{line_v:.2f}|1"
    return odds_id, selection_id, "0"


# 页面上下文执行：先干跑 all-odds-selections 验证 oddsId + 拿规范 selectionId，
# 再 buyV4 提交（两步均为页面 fetch，带 cookie 同源）
_JS_PLACE = """
async (payload) => {
  const uuid = () => {
    const h = '0123456789abcdef';
    let s = '';
    for (let i = 0; i < 36; i++) {
      if (i === 8 || i === 13 || i === 18 || i === 23) s += '-';
      else s += h[Math.floor(Math.random() * 16)];
    }
    return s;
  };
  const ts = () => `${Date.now()}${Math.floor(Math.random() * 900 + 100)}`;
  // 1) 干跑：验证 oddsId 可被服务器识别，读回规范 selectionId 与实时赔率
  const probe = {
    oddsFormat: 1,
    oddsId: payload.oddsId,
    oddsSelectionsType: 'NORMAL',
    selectionId: payload.probeSelectionId,
  };
  let verified = null;
  try {
    const r1 = await fetch(`/member-betslip/v2/all-odds-selections?locale=zh_CN&_=${ts()}&withCredentials=true`, {
      method: 'POST', credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ oddsSelections: [probe] }),
    });
    const t1 = await r1.text();
    let j1 = null;
    try { j1 = JSON.parse(t1); } catch (e) {}
    verified = { status: r1.status, body: t1.slice(0, 2000), json: j1 };
  } catch (e) {
    return { stage: 'probe', error: String(e), status: 0 };
  }
  if (verified.status !== 200) {
    return { stage: 'probe', status: verified.status, body: verified.body, rejected: true };
  }
  // 从响应提取规范 selectionId（结构可能为数组包一层）
  let canonicalSel = null;
  try {
    const d = verified.json && (verified.json.data || verified.json);
    const arr = Array.isArray(d) ? d : (d && (d.oddsSelections || d.selections || []));
    const item = Array.isArray(arr) && arr.find((x) => x && (x.selectionId || x.oddsId));
    if (item) canonicalSel = item.selectionId || null;
  } catch (e) {}
  // 2) 提交 buyV4（canonical selectionId 优先）
  const sel = { ...payload.selection };
  if (canonicalSel) sel.selectionId = canonicalSel;
  const rid = uuid();
  const r2 = await fetch(`/bet-placement/buyV4?uniqueRequestId=${rid}&locale=zh_CN&_=${ts()}&withCredentials=true`, {
    method: 'POST', credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      'X-Requested-With': 'XMLHttpRequest',
    },
    body: JSON.stringify({
      acceptBetterOdds: payload.acceptBetterOdds,
      oddsFormat: 1,
      selections: [sel],
    }),
  });
  const t2 = await r2.text();
  let j2 = null;
  try { j2 = JSON.parse(t2); } catch (e) {}
  return {
    stage: 'place', status: r2.status, body: t2.slice(0, 1500), json: j2, rid,
    probeBody: verified.body.slice(0, 500), canonicalSel,
  };
}
"""


def _probe_selection_id(odds_id: str) -> str:
    """构造干跑用的 selectionId：{伪id}|{oddsId各段}|0。服务器识别的是 oddsId。"""
    return f"0|{odds_id}|0"


async def api_place_pinnacle(
    page: Any,
    *,
    base_url: str,
    session_token: str,
    match_external_id: str,
    selection: str,
    odds: float,
    stake: Decimal,
    bet_type: str,
    odds_data: dict,
) -> Optional[Any]:
    """
    API 下单：成功/明确失败返回 PlaceBetResult；None 表示无法构造（回退 UI）。
    """
    from app.services.bookmakers.base import PlaceBetResult

    _ = base_url, session_token
    line_hint = odds_data.get("line") or odds_data.get("_line")
    ids = _build_ids(match_external_id, selection, line_hint, odds_data)
    if not ids:
        logger.info(
            "pinnacle api place: skip (no ids) mid=%s sel=%s odds_data_keys=%s",
            match_external_id, selection, list((odds_data or {}).keys()),
        )
        return None
    odds_id, selection_id, _period = ids

    req_id = str(uuid.uuid4())
    payload = {
        "acceptBetterOdds": True,
        "oddsId": odds_id,
        "probeSelectionId": _probe_selection_id(odds_id),
        "selection": {
            "odds": f"{float(odds):.3f}",
            "oddsId": odds_id,
            "selectionId": selection_id,
            "stake": float(stake),
            "uniqueRequestId": req_id,
            "wagerType": "NORMAL",
            "betLocationTracking": dict(_BET_LOCATION_TRACKING),
            "winRiskStake": "RISK",
        },
    }

    try:
        res = await asyncio.wait_for(
            page.evaluate(_JS_PLACE, payload), timeout=20.0
        )
    except Exception as e:
        logger.warning("pinnacle api place: evaluate failed: %s", e)
        return None  # 回退 UI

    if not isinstance(res, dict):
        return None

    stage = str(res.get("stage") or "")
    if stage == "probe":
        # 干跑被拒：oddsId 构造有误，安全回退 UI（未下单无风险）
        logger.info(
            "pinnacle api place probe rejected: status=%s body=%s oddsId=%s",
            res.get("status"), str(res.get("body") or "")[:200], odds_id,
        )
        return None

    status = int(res.get("status") or 0)
    body = str(res.get("body") or "")[:500]
    j = res.get("json")

    # 成功判定：status 200 且响应含成功标志
    ok = False
    ext_bet = ""
    if status == 200 and isinstance(j, dict):
        data = j.get("data") if isinstance(j.get("data"), dict) else j
        # 常见成功结构：{data: {bettingItems[...]}} 或 {success: true}
        if isinstance(data, dict):
            items = data.get("bettingItems") or data.get("betResults") or []
            if isinstance(items, list) and items:
                first = items[0] if isinstance(items[0], dict) else {}
                ext_bet = str(first.get("bettingItemId") or first.get("id") or "")
                ok = True
            elif data.get("success") is True:
                ok = True
    if ok:
        logger.info(
            "pinnacle api place ok: mid=%s sel=%s stake=%s odds=%s oddsId=%s bet=%s",
            match_external_id, selection, stake, odds, odds_id, ext_bet or "-",
        )
        return PlaceBetResult(
            ok=True,
            message=f"api_ok|{odds_id}|{ext_bet}",
            external_bet_id=ext_bet or None,
        )

    # 明确失败（status!=200 或结构异常）：带原文返回，不回退 UI（防止 UI 再点一次重复下单）
    logger.warning(
        "pinnacle api place failed: status=%s body=%s oddsId=%s",
        status, body, odds_id,
    )
    return PlaceBetResult(
        ok=False,
        message=f"api_fail|status={status}|{body[:200]}",
    )
