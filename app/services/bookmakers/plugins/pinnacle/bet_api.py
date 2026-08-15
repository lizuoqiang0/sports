"""平博 API 下单器：页面上下文实时拉盘 + fetch buyV4（替代 UI 点击）。

逆向自真实成交抓包（2026-08-15）：

  实时盘:  GET /sports-service/sv/compact/events?...mk=2（滚球）
           → ev[0]=mid, ev[8]["0"] periods, p0[1] 大小线列表
           → tot_row = [线, ?, over, under, sel_id, ?, ?, ..., alt]
  验证:    POST /member-betslip/v2/all-odds-selections
           req: {oddsSelections:[{oddsFormat:1, oddsId, oddsSelectionsType:"NORMAL", selectionId}]}
  提交:    POST /bet-placement/buyV4?uniqueRequestId=...&locale=zh_CN&withCredentials=true
           req: {acceptBetterOdds, oddsFormat:1, selections:[{odds, oddsId, selectionId,
                stake, uniqueRequestId, wagerType:"NORMAL", betLocationTracking, winRiskStake:"RISK"}]}

oddsId 编码（全部来自成功样本）：{mid}|0|3|{period}|1|{线}
  - market=3 恒为大小球
  - period 段：抓包 3 或 4（含义未完全确定，滚球实测用 3）
  - 末段线必须用「实时」值（DB 快照会过期——女王公园 DB total=5.25 但实下 4.25）
selectionId: 干跑响应回读规范值，缺失时按 9 段格式构造。
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from decimal import Decimal
from typing import Any, Optional

logger = logging.getLogger(__name__)

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

# 页面内一条龙：实时拉滚球盘 → 定位目标 mid 的大小线 → 构造 → 干跑验证 → buyV4
_JS_API_BET = r"""
async (args) => {
  const mid = String(args.mid || '');
  const side = String(args.side || 'under');   // over | under
  const stake = Number(args.stake || 0);
  const wantOdds = Number(args.odds || 0);      // 决策时赔率（对照用）
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
  const post = async (url, body) => {
    const r = await fetch(url, {
      method: 'POST', credentials: 'include',
      headers: {
        'Content-Type': 'application/json',
        'X-Requested-With': 'XMLHttpRequest',
        'Accept': 'application/json, text/plain, */*',
      },
      body: JSON.stringify(body),
    });
    const t = await r.text();
    let j = null;
    try { j = JSON.parse(t); } catch (e) {}
    return { status: r.status, body: t, json: j };
  };

  // 1) 实时拉滚球盘：优先复用页面真实请求过的 compact URL 模板（自拼参数可能被服务端忽略），
  //    将 sp 参数替换为目标球种；无模板时退回手拼
  const mkQs = [
    'lg=', 'ev=', 'mk=2', 'btg=1', 'ot=1', 'd=', 'o=0', 'l=2',
    'v=', 'lv=', 'me=0', 'more=false', 'tm=0', 'pa=0', 'c=Others', 'pn=-1',
    'cl=-1', 'hle=true', 'inl=false', 'pv=1', 'ic=false', 'ice=false',
    'withCredentials=true', 'lang=zh_CN',
  ].join('&');
  const discovered = [];
  try {
    for (const e of performance.getEntriesByType('resource')) {
      const n = e.name || '';
      if (/sports-service\/sv\/compact\/events/i.test(n)) {
        const u = n.split('#')[0];
        if (discovered.indexOf(u) < 0) discovered.push(u);
      }
    }
  } catch (e) {}
  const buildUrls = (sp) => {
    const urls = [];
    for (const d of discovered.slice(0, 4)) {
      try {
        const u = new URL(d);
        u.searchParams.set('sp', String(sp));
        u.searchParams.set('mk', '2');
        u.searchParams.set('l', '2');
        urls.push(u.toString());
      } catch (e) {}
    }
    urls.push(`${location.origin}/sports-service/sv/compact/events?sp=${sp}&${mkQs}`);
    return urls;
  };
  let target = null;   // { line, over, under, selId, period }
  const sportsTried = [];
  let liveMids = [];   // 调试：拉到的滚球 mid 样本
  let totalEvents = 0;
  for (const sp of (args.sportIds || [29, 4])) {
    sportsTried.push(sp);
    for (const url of buildUrls(sp)) {
      let j = null;
      try {
        // 关键：compact 拉取不带 cookie（登录态 cookie 会被 sports-service 400 拒，见 odds.py）
        const r = await fetch(url, { credentials: 'omit', headers: { accept: 'application/json' } });
        if (r.ok) j = await r.json();
      } catch (e) { continue; }
      if (!j || !Array.isArray(j.l)) continue;
      for (const sportBlock of j.l) {
        if (!Array.isArray(sportBlock) || sportBlock.length < 3) continue;
        for (const lg of (sportBlock[2] || [])) {
          if (!Array.isArray(lg) || lg.length < 3) continue;
          for (const ev of (lg[2] || [])) {
            if (!Array.isArray(ev)) continue;
            totalEvents++;
            if (liveMids.length < 8) liveMids.push(String(ev[0]));
            if (String(ev[0]) !== mid) continue;
            const periods = (ev[8] && typeof ev[8] === 'object') ? ev[8] : {};
            const p0 = periods['0'] || periods[0];
            if (!Array.isArray(p0)) continue;
            // p0[1] 大小线列表: [线, 线, over, under, selId, 主盘标志, ?, ?]
            // row[5]=='1' 是主盘（实时线）；'0' 是历史/次线。选错会 INVALID_REQUEST_DATA
            const rows = Array.isArray(p0[1]) ? p0[1] : [];
            let mainRow = null;
            for (const row of rows) {
              if (!Array.isArray(row) || row.length < 5) continue;
              if (String(row[5]) === '1') { mainRow = row; break; }
              if (!mainRow) mainRow = row;
            }
            if (!mainRow) continue;
            // compact 返回的线/赔率可能是字符串，统一转数字
            const line = Number(mainRow[0]);
            const over = Number(mainRow[2]);
            const under = Number(mainRow[3]);
            const selId = String(mainRow[4] || '');
            const isLive = !!(ev[5] || ev[6] || ev[15] || ev[16]);
            target = { line, over, under, selId, isLive };
          }
        }
      }
      if (target) break;
    }
    if (target) break;
  }
  if (!target) {
    return { stage: 'odds', error: 'match_or_total_not_found', mid, sportsTried, totalEvents, liveMids };
  }

  // 2) 构造 oddsId（period: 滚球=3，其余=4，基于成功样本归纳）
  const period = target.isLive ? 3 : 4;
  const lineStr = String(target.line);
  const oddsId = `${mid}|0|3|${period}|1|${lineStr}`;
  const price = side === 'over' ? target.over : target.under;
  if (!price || price <= 1) {
    return { stage: 'odds', error: 'price_invalid', target, oddsId };
  }

  // 3) 直接 buyV4 提交（构造 selectionId；服务器格式错会 400 明确拒绝，不会误下单）
  //    selectionId 精确结构（成功样本逆向）：
  //      9段 = 裸id | mid | s2 | 3 | period | 1 | line2位 | flag
  //      其中 (mid, s2, 3, period, 1, line) 就是 oddsId 的 6 段原样，
  //      flag = s2（女王公园滚球样本 s2=0 flag=0；17:14 样本 s2=1 flag=1）
  //    period 推断：滚球=3，非滚球=4（样本归纳，多候选重试兜底）
  const cands = [];
  const line2 = Number(target.line).toFixed(2);
  // selectionId 段5 = 方向位（成功样本逆向：女王公园under样本=0，17:14 over样本=1）
  const dirSeg = side === 'over' ? '1' : '0';
  const mkSel = (s2v, per) => `${target.selId || 0}|${mid}|${s2v}|3|${per}|${dirSeg}|${line2}|${s2v}`;
  const selMain = String(target.selId || '');
  if (selMain) {
    for (const s2v of ['0', '1']) {
      for (const per of ['0', '3', '4']) {
        cands.push(mkSel(s2v, per));
      }
    }
  }
  const triedFormats = [];
  let lastPlace = null;
  for (const cand of cands) {
    // oddsId/selectionId 同构：s2、period、线段完全一致；线段统一 2 位小数
    // （compact 大线值可能是 "104" 整数形式，真实线 104.5——必须规范化）
    const seg = cand.split('|');
    const oid = `${mid}|${seg[2]}|3|${seg[4]}|1|${line2}`;
    const selPayload = {
      odds: price.toFixed(3),
      oddsId: oid,
      selectionId: cand,
      stake,
      uniqueRequestId: uuid(),
      wagerType: 'NORMAL',
      betLocationTracking: args.tracking || {},
      winRiskStake: 'RISK',
    };
    const place = await post(
      `/bet-placement/buyV4?uniqueRequestId=${uuid()}&locale=zh_CN&_=${ts()}&withCredentials=true`,
      { acceptBetterOdds: true, oddsFormat: 1, selections: [selPayload] },
    );
    triedFormats.push({ selId: cand.slice(0, 70), oddsId: oid, status: place.status, body: place.body.slice(0, 100) });
    lastPlace = { ...place, _oid: oid };
    // 200 且业务成功（无 errorCode）= 成交
    const bizOk = place.status === 200 && place.body.indexOf('errorCode') < 0;
    if (bizOk) {
      return {
        stage: 'place', status: place.status, body: place.body.slice(0, 1500),
        json: place.json, oddsId: oid, price, line: target.line,
        selId: target.selId, canonicalSel: cand, via: 'direct',
        liveOdds: { over: target.over, under: target.under }, wantOdds,
        triedFormats,
      };
    }
    // 业务拒绝（200+errorCode）→ 继续尝试下一候选格式
    if (place.status === 200) continue;
    // 非 400 的失败（403/5xx）不必再试其他格式
    if (place.status !== 400) break;
  }
  return {
    stage: 'place', status: lastPlace ? lastPlace.status : 0,
    body: (lastPlace && lastPlace.body || '').slice(0, 1500),
    json: lastPlace ? lastPlace.json : null,
    oddsId: lastPlace ? lastPlace._oid : '', price, line: target.line,
    selId: target.selId, canonicalSel: null, via: 'direct',
    liveOdds: { over: target.over, under: target.under }, wantOdds,
    triedFormats,
  };
}
"""


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
    """API 下单：成功/明确失败返回 PlaceBetResult；None 表示无法走 API（回退 UI）。"""
    from app.services.bookmakers.base import PlaceBetResult

    _ = base_url, session_token, odds_data
    mid = str(match_external_id or "")
    if mid.startswith("pinnacle:"):
        mid = mid.split(":", 1)[1]
    side = (selection or "").lower()
    if side not in ("over", "under") or bet_type not in ("total", "TOTAL"):
        return None  # 仅支持大小球；让球/独赢回退 UI

    # sport id（compact: 29=足球 4=篮球，见 odds.py SPORT_IDS）
    sport_ids = [29, 4]
    try:
        sid = int(str((odds_data or {}).get("_site", {}).get("sport_id") or 0))
        if sid in (29, 4):
            sport_ids = [sid, 29, 4]
    except Exception:
        pass

    args = {
        "mid": mid,
        "side": side,
        "stake": float(stake),
        "odds": float(odds or 0),
        "sportIds": sport_ids[:3],
        "tracking": dict(_BET_LOCATION_TRACKING),
        "homeTok": str(odds_data.get("_home") or "")[:24] if isinstance(odds_data, dict) else "",
        "awayTok": str(odds_data.get("_away") or "")[:24] if isinstance(odds_data, dict) else "",
    }

    try:
        res = await asyncio.wait_for(page.evaluate(_JS_API_BET, args), timeout=30.0)
    except Exception as e:
        logger.warning("pinnacle api place: evaluate failed: %s", e)
        return None

    if not isinstance(res, dict):
        return None
    stage = str(res.get("stage") or "")

    if stage == "odds":
        # 未下单：实时盘没找到 → 安全回退 UI
        logger.info(
            "pinnacle api place odds: %s | mid=%s events=%s mids=%s",
            res.get("error"), mid, res.get("totalEvents"), res.get("liveMids"),
        )
        return None

    status = int(res.get("status") or 0)
    body = str(res.get("body") or "")[:600]
    j = res.get("json")
    live_price = float(res.get("price") or 0)
    want = float(res.get("wantOdds") or 0)

    # 200 = 已提交：语义上 success 或含 bettingItems 才算成交
    ok = False
    ext_bet = ""
    if status == 200 and isinstance(j, dict):
        data = j.get("data") if isinstance(j.get("data"), dict) else j
        if isinstance(data, dict):
            items = data.get("bettingItems") or data.get("betResults") or []
            if isinstance(items, list) and items:
                first = items[0] if isinstance(items[0], dict) else {}
                ext_bet = str(first.get("bettingItemId") or first.get("id") or "")
                ok = True
            elif data.get("success") is True:
                ok = True
            else:
                # 200 但无成功标志：可能全部格式被业务层拒绝（如下线/停盘）
                ok = False

    if ok:
        logger.info(
            "pinnacle api place ok: mid=%s side=%s stake=%s live_price=%s(want=%s) oddsId=%s bet=%s",
            mid, side, stake, live_price, want, res.get("oddsId"), ext_bet or "-",
        )
        return PlaceBetResult(
            ok=True,
            message=f"api_ok|{res.get('oddsId')}|{ext_bet}",
            external_bet_id=ext_bet or None,
        )

    # 全部格式失败：业务拒绝(200+errorCode)或非400 → 已尽力，返回明确失败
    # （多个候选都试过后仍拒绝，多为盘口关闭/限额/风控，UI 兜底大概率同样被拒，
    #   但 UI 有机会走人工确认流程，交给上层重试策略）
    tried = res.get("triedFormats") or []
    logger.warning(
        "pinnacle api place failed: status=%s body=%s oddsId=%s tried=%s",
        status, body[:200], res.get("oddsId"), tried,
    )
    return PlaceBetResult(
        ok=False,
        message=f"api_fail|status={status}|{body[:200]}",
    )
