"""平博 DOM 下单兜底。"""
from __future__ import annotations

import asyncio
import logging
import re
from decimal import Decimal

from app.services.bookmakers.plugins.pinnacle.modals import (
    cleanup_pinnacle_failed_slips,
    dismiss_pinnacle_blocking_modals,
)

logger = logging.getLogger(__name__)


def _to_f(v) -> float:
    """安全转 float（金额读回校验用）。"""
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return -1.0


def _build_line_aliases(line: float | None) -> list[str]:
    """生成盘口线别名（与 click_js lineAliases 逻辑一致）。

    四分线 3.75 → ["3.75", "3.5-4"]，平博页面显示 3.5-4。
    """
    if line is None:
        return []
    try:
        n = float(line)
    except (TypeError, ValueError):
        return [str(line)]
    if n <= 0:
        return [str(line)]
    lo = int(n)
    frac = n - lo
    if abs(frac - 0.25) < 1e-9:
        return [str(n), f"{lo}-{lo + 0.5}"]
    if abs(frac - 0.75) < 1e-9:
        return [str(n), f"{lo + 0.5}-{lo + 1}"]
    if abs(frac) < 1e-9:
        # 篮球整数总分在 API 中常为 157，页面显示为 157.0。
        return [str(n), str(lo)]
    return [str(n)]


def _norm_market_text(value: str) -> str:
    """Normalize sportsbook text without collapsing decimal boundaries."""
    return re.sub(r"[\s\u200e\u200f]+", "", str(value or "")).lower()


def _has_exact_line(text: str, line: float | None) -> bool:
    """Return whether *text* contains the requested line as a numeric token.

    A substring check is unsafe here: line ``2.5`` also matches an unrelated
    price ``2.500``.  That was one of the ways the compact board could select
    the handicap cell next to the requested total.
    """
    raw = str(text or "")
    for alias in _build_line_aliases(line):
        pattern = rf"(?<![0-9.\-]){re.escape(alias)}(?![0-9.\-])"
        if re.search(pattern, raw, re.I):
            return True
    return False


def _has_slip_direction(text: str, selection: str) -> bool:
    """Require a real selection label, not the 大/小 inside ``大小盘``."""
    raw = str(text or "")
    lowered = raw.lower()
    # Remove market headings before looking for a one-character Chinese side.
    chinese = re.sub(r"大小(?:盘|球)?", "", raw)
    if selection == "under":
        return bool(
            "小" in chinese
            or re.search(r"\bunder\b", lowered)
            or "低于" in raw
        )
    return bool(
        "大" in chinese
        or re.search(r"\bover\b", lowered)
        or "高于" in raw
    )


def _validate_pinnacle_slip_text(
    text: str,
    *,
    home: str,
    away: str,
    selection: str,
    line: float | None,
    bet_count: int | None = None,
    team_aliases: tuple[str, ...] | list[str] = (),
) -> tuple[bool, str]:
    """Fail-closed validation of the Pinnacle slip before any confirmation."""
    raw = str(text or "").strip()
    if not raw:
        return False, "slip_snapshot_missing"

    count_match = re.search(r"投下\s*(\d+)\s*注", raw)
    detected_count = bet_count
    if detected_count is None and count_match:
        detected_count = int(count_match.group(1))
    if detected_count is not None and detected_count != 1:
        return False, f"slip_multiple_bets:{detected_count}"

    lowered = raw.lower()
    market_text = lowered
    team_names = tuple(x for x in (home, away, *team_aliases) if str(x or "").strip())
    for team_name in team_names:
        if team_name:
            market_text = market_text.replace(str(team_name).lower(), "")
    if any(
        word in market_text
        for word in ("让球盘", "让球", "让分", "handicap", "spread", "亚盘")
    ):
        return False, "slip_market_mismatch:handicap"
    if any(
        word in market_text
        for word in (
            "主队总", "客队总", "球队总", "team total", "角球", "corners",
            "罚牌", "黄牌", "红牌", "cards", "球员", "player",
        )
    ):
        return False, "slip_market_mismatch:non_match_total"
    if any(
        word in lowered
        for word in (
            "上半场", "下半场", "第一节", "第二节", "第三节", "第四节",
            "first half", "second half", "1st half", "2nd half",
        )
    ):
        return False, "slip_period_mismatch"

    if not _has_exact_line(raw, line):
        return False, "slip_line_mismatch"
    if not _has_slip_direction(raw, selection):
        return False, "slip_direction_mismatch"

    norm = _norm_market_text(raw)
    normalized_teams = [_norm_market_text(name) for name in team_names]
    if not any(len(name) >= 2 and name in norm for name in normalized_teams):
        return False, "slip_team_mismatch"

    total_hint = any(
        word in lowered
        for word in (
            "大小盘", "大小球", "全场大小", "总进球", "进球数", "总分",
            "over/under", "over-under", "totals", "total",
        )
    )
    # Some compact slips omit the market heading and only show 小/大 + line.
    # Direction + exact line is sufficient only after handicap/period rejection.
    if not total_hint and not _has_slip_direction(raw, selection):
        return False, "slip_total_market_missing"
    return True, "ok"


async def ui_place_pinnacle_total(
    page,
    *,
    home: str,
    away: str,
    selection: str,
    odds: float,
    stake: Decimal,
    line: float | None,
    sport: str = "",
    event_id: str = "",
    team_aliases: tuple[str, ...] | list[str] = (),
    live_only: bool = True,
    dynamic_stake: Decimal | None = None,
    stake_cap: Decimal | None = None,
    available_balance: Decimal | None = None,
    preview_only: bool = False,
) -> tuple[bool, str, Decimal, str]:
    """
    平博 DOM 兜底：定位赛事行/盘口线 → 点全场大小球赔率 → 填金额并确认。
    返回 (clicked_confirm, detail, actual_stake, bet_ref)。
    bet_ref: 从确认弹窗/URL 中提取的站点订单号，空字符串表示未提取到。
    """
    sel = (selection or "").lower()
    # 大小球双向 DOM 点选。反方向词按方向取（under 防误点大、over 防误点小）。
    # 注意：不含单字母 o/u —— ctx.includes("o") 会命中任意英文文本，side 校验失效
    if sel in ("under", "u"):
        side_words = ["小", "under", "低于"]
    elif sel in ("over", "o"):
        side_words = ["大", "over", "高于"]
    else:
        return False, "仅支持大小球", Decimal("0"), ""
    sport_l = (sport or "").lower()
    if not sport_l:
        # 从当前 URL 猜
        try:
            cur0 = (page.url or "").lower()
            if "basket" in cur0:
                sport_l = "basketball"
            else:
                sport_l = "football"
        except Exception:
            sport_l = "football"

    # 下单前必须确认页面不是维护页/白屏且所有安全推广遮挡已关闭。
    # 恢复失败时 fail-closed，绝不继续点击任何赔率或确认按钮。
    try:
        from app.services.bookmakers.plugins.pinnacle.venue import (
            ensure_pinnacle_page_ready,
        )

        if not await ensure_pinnacle_page_ready(
            page, attempts=2, venue_url=str(page.url or "")
        ):
            return False, "pinnacle_page_recovery_failed", Decimal("0"), ""
    except Exception as e:
        logger.warning("pinnacle ui: page readiness check failed: %s", e)
        return False, "pinnacle_page_readiness_error", Decimal("0"), ""

    # 队名 token：本地合并名 + 平博采盘原生名。原生名优先，避免
    # NowScore/平博翻译不一致导致整页 tokHit=[]。
    tokens = []
    alias_names = [str(x or "").strip() for x in team_aliases if str(x or "").strip()]
    for t in (*alias_names, home, away):
        t = (t or "").strip()
        if not t:
            continue
        tokens.append(t)
        for n in (2, 3, 4, 5, 6):
            if len(t) >= n:
                tokens.append(t[:n])
                tokens.append(t[-n:])
        # 规范化 token：去掉常见后缀/前缀后生成额外搜索 token
        import re as _re
        nt = t.lower().replace(" ", "").replace("\u200e", "").replace("\u200f", "")
        nt = _re.sub(r"[\(（][^\)）]*[\)）]", "", nt)
        for suf in ("足球俱乐部", "足球队", "俱乐部", "fc", "cf", "sc", "afc", "队", "女足", "男足", "青年", "后备", "预备队", "二队", "b队"):
            if nt.endswith(suf) and len(nt) > len(suf) + 1:
                nt2 = nt[:-len(suf)]
                if len(nt2) >= 2:
                    tokens.append(nt2)
                    for n in (2, 3, 4):
                        if len(nt2) >= n:
                            tokens.append(nt2[:n])
                break
        nt = _re.sub(r"\d{2,4}$", "", nt)
        if nt and nt != t.lower().replace(" ", ""):
            tokens.append(nt)
    seen: set[str] = set()
    tokens = [x for x in tokens if not (x in seen or seen.add(x))]

    # 关闭所有已知遮挡提示；确认投注/清空注单两个业务弹窗受保护，绝不在
    # 这里点 OK/好的。该处理与采盘共用，避免公告或失败提示挡住赔率列表。
    await dismiss_pinnacle_blocking_modals(page)

    # 生产自动投注固定滚球；preview 可在没有滚球时对真实赛前 UI 做安全
    # 选择器演练，但仍停在金额输入、绝不进入提交代码。
    try:
        cur_url = (page.url or "").lower()
        wanted_sport = "basketball" if sport_l == "basketball" else "soccer"
        wanted_path = f"/{wanted_sport}/live" if live_only else f"/{wanted_sport}"
        route_ok = wanted_path in cur_url and (live_only or "/live" not in cur_url)
        if not route_ok:
            from urllib.parse import urlparse

            pu = urlparse(page.url or "")
            org = f"{pu.scheme}://{pu.netloc}" if pu.netloc else ""
            if org:
                try:
                    await page.goto(
                        f"{org}/zh-cn/compact/sports/{wanted_sport}{'/live' if live_only else ''}",
                        wait_until="domcontentloaded",
                        timeout=45000,
                    )
                    await page.wait_for_timeout(4000)
                except Exception as e:
                    logger.warning("pinnacle ui: sport goto err: %s", e)
                logger.info("pinnacle ui: sport page ready url=%s", (page.url or "")[:100])
    except Exception as e:
        logger.warning("pinnacle ui: basketball nav failed: %s", e)

    # 兼容历史赔率行没有 event_home/event_away 的情况：用采盘的 mid 从本页
    # 已访问过的 compact/events GET 响应恢复平博原生队名。这里只读取赛事，
    # 不调用任何下注接口。
    if event_id and not alias_names:
        resolve_event_js = r"""async (mid) => {
          const urls = [];
          try {
            for (const item of performance.getEntriesByType('resource')) {
              const url = String(item.name || '');
              if (/sports-service\/sv\/compact\/events/i.test(url) && !urls.includes(url)) urls.push(url);
            }
          } catch (e) {}
          const find = (body) => {
            if (!body || !Array.isArray(body.l)) return null;
            for (const sport of body.l) {
              if (!Array.isArray(sport) || !Array.isArray(sport[2])) continue;
              for (const league of sport[2]) {
                if (!Array.isArray(league) || !Array.isArray(league[2])) continue;
                for (const ev of league[2]) {
                  if (!Array.isArray(ev) || String(ev[0] || '') !== String(mid)) continue;
                  return {
                    home: String(ev[1] || '').trim(),
                    away: String(ev[2] || '').trim(),
                    league: String(league[1] || '').trim(),
                  };
                }
              }
            }
            return null;
          };
          for (const url of urls.slice(-8).reverse()) {
            try {
              const response = await fetch(url, { credentials: 'include', headers: { accept: 'application/json' } });
              if (!response.ok) continue;
              const hit = find(await response.json());
              if (hit && hit.home && hit.away) return hit;
            } catch (e) {}
          }
          return null;
        }"""
        try:
            resolved = await asyncio.wait_for(
                page.evaluate(resolve_event_js, str(event_id)), timeout=12.0
            )
        except Exception as e:
            resolved = None
            logger.debug("pinnacle native event alias resolve failed mid=%s: %s", event_id, e)
        if isinstance(resolved, dict):
            for name in (resolved.get("home"), resolved.get("away")):
                clean_name = str(name or "").strip()
                if not clean_name or clean_name in alias_names:
                    continue
                alias_names.append(clean_name)
                tokens.insert(0, clean_name)
                for size in (2, 3, 4, 5, 6):
                    if len(clean_name) >= size:
                        tokens.extend((clean_name[:size], clean_name[-size:]))
            seen.clear()
            tokens = [x for x in tokens if not (x in seen or seen.add(x))]
            if alias_names:
                logger.info(
                    "pinnacle event aliases resolved mid=%s aliases=%s",
                    event_id,
                    alias_names[:2],
                )

    click_js = """(args) => {
              const { tokens, odds, sideWords, line, homeN, awayN, selDir, eventId } = args;
              // 清除空白 + U+200E/U+200F（平博 RTL 布局零宽标记），否则 indexOf/includes 失效
              const norm = (s) => String(s || '').replace(/[\\s\\u200e\\u200f]/g, '').toLowerCase();
              const oddsTextOk = (t) => {
                const n = Number(String(t || '').replace(/[^0-9.]/g, ''));
                if (!n || n < 1.01 || n > 50) return false;
                // 滚球赔率漂移常见：放宽到 ±0.15 匹配（后续由 decide_odds_change 校验是否接受）
                return Math.abs(n - Number(odds)) <= 0.15;
              };
              const lineTxt = (line == null || line === '') ? '' : String(line);
              // 四分之一线区间别名：DB 记 1.25/2.75，平博页面显示 1-1.5 / 2.5-3。
              // 仅按 lineTxt 精确匹配会找不到盘口（实测 odds_not_found 根因）
              const lineAliases = (() => {
                if (!lineTxt) return [];
                const n = Number(lineTxt);
                if (!n || n !== n || n <= 0) return [lineTxt];
                const lo = Math.floor(n);
                const frac = n - lo;
                if (Math.abs(frac - 0.25) < 1e-9) return [lineTxt, `${lo}-${lo + 0.5}`];
                if (Math.abs(frac - 0.75) < 1e-9) return [lineTxt, `${lo + 0.5}-${lo + 1}`];
                if (Math.abs(frac) < 1e-9) return [lineTxt, `${lo}.0`];
                return [lineTxt];
              })();
              // Line aliases contain only digits, dots and a range dash.
              const escRe = (s) => String(s || '').split('.').join('[.]');
              const hitLineTxt = (ctx) => !lineTxt || lineAliases.some((a) => {
                // Numeric-token match: 2.5 must never match an unrelated price 2.500.
                const re = new RegExp('(^|[^0-9.\\-])' + escRe(a) + '([^0-9.\\-]|$)', 'i');
                return re.test(String(ctx || ''));
              });
              const nodes = Array.from(document.querySelectorAll('div, tr, li, section, article, a'));
              let row = null;
              let how = '';
              let bestScore = -1e9;
              // 首选采盘返回的平博赛事 mid。只接受属性/href 中的完整数字 token，
              // 再向上寻找含赔率的单场容器，避免相似队名和翻译差异。
              if (eventId && /^\\d{5,24}$/.test(String(eventId))) {
                const idRe = new RegExp('(^|[^0-9])' + String(eventId) + '([^0-9]|$)');
                const anchors = Array.from(document.querySelectorAll(
                  '[data-event-id], [data-eventid], [data-event], [data-id], [id], a[href]'
                ));
                for (const anchor of anchors) {
                  const attrs = [
                    anchor.getAttribute('data-event-id'), anchor.getAttribute('data-eventid'),
                    anchor.getAttribute('data-event'), anchor.getAttribute('data-id'),
                    anchor.getAttribute('id'), anchor.getAttribute('href'),
                  ].filter(Boolean).join(' ');
                  if (!idRe.test(attrs)) continue;
                  for (let el = anchor, depth = 0; el && depth < 9; el = el.parentElement, depth++) {
                    const raw = String(el.innerText || '');
                    if (!raw || raw.length < 8 || raw.length > 900) continue;
                    if (/投注单|总注金|最低投注额|投下\\s*\\d+\\s*注|place\\s*bet|stake/i.test(raw)) continue;
                    const oddsHits = (raw.match(/(?:^|\\s)(\\d{1,2}\\.\\d{2,3})(?:\\s|$)/g) || []).length;
                    if (oddsHits < 1 || oddsHits > 12) continue;
                    const score = oddsHits * 100 - Math.abs(raw.length - 160);
                    if (score > bestScore) { row = el; bestScore = score; how = 'eventId:' + eventId; }
                  }
                }
              }
              if (!row) for (const tok of (tokens || [])) {
                const tn = norm(tok);
                if (!tn || tn.length < 2) continue;
                for (const el of nodes) {
                  try {
                    const raw = el.innerText || '';
                    const t = norm(raw);
                    if (!t || t.length < 8 || t.length > 900) continue;
                    // Bet-slip text repeats team/line/odds and can be smaller than the
                    // real event row.  It is never a valid source for an odds click.
                    if (/投注单|总注金|最低投注额|最高投注额|投下\\s*\\d+\\s*注|移除全部|刷新选择的注单|place\\s*bet|stake/i.test(raw)) continue;
                    if (!t.includes(tn)) continue;
                    // 必须像「赛事行」：含赔率数字；避免点到纯队名短节点
                    const oddsHits = (raw.match(/(?:^|\\s)(\\d\\.\\d{2,3})(?:\\s|$)/g) || []).length;
                    if (oddsHits < 1) continue;
                    // 偏好含赔率、长度适中的行；赔率格数 >10 视为联赛区块（含多场
                    // 比赛），重罚 —— 否则 odds21 的区块会压过真正的单场行
                    const effHits = Math.min(oddsHits, 10);
                    const excessPenalty = oddsHits > 10 ? (oddsHits - 10) * 80 : 0;
                    const score = effHits * 100 - excessPenalty - Math.abs(raw.length - 160);
                    if (score > bestScore) {
                      bestScore = score;
                      row = el; how = 'token:' + tok + ':odds' + oddsHits;
                    }
                  } catch (e) {}
                }
                if (row) break;
              }
              if (!row && lineTxt) {
                for (const el of nodes) {
                  try {
                    const raw = String(el.innerText || '');
                    if (/投注单|总注金|最低投注额|最高投注额|投下\\s*\\d+\\s*注|移除全部|刷新选择的注单|place\\s*bet|stake/i.test(raw)) continue;
                    if (!hitLineTxt(raw)) continue;
                    if (raw.length < 4 || raw.length > 900) continue;
                    // line 兜底也必须含队名：盘口线数字（如2.5）全站大量重复，禁止点错行
                    const rawN = norm(raw);
                    const hasTeam = (tokens || []).some((tk) => {
                      const t3 = norm(tk);
                      return t3 && t3.length >= 3 && rawN.includes(t3);
                    });
                    if (!hasTeam) continue;
                    if (!row || raw.length < (row.innerText || '').length) {
                      row = el; how = 'line:' + lineTxt;
                    }
                  } catch (e) {}
                }
              }
              let target = null;
              let rowFallback = null;
              let pureOdds = null;
              // 滚球赔率漂移兜底：快照 odds ±0.06 找不到时，行内按 side+line 的任意赔率节点
              let sideLoose = null;
              // 行级队名校验：目标行必须真正包含目标队名，否则禁止任何点击
              // （防「陶朗加市」2字片段误中「普雷斯顿」等无关行的赔率）。
              // 完整队名任意长度均可命中（2字队名如「坎昆」此前永远过不了≥3校验），
              // 片段仍要求 ≥3 字防误中。
              let rowHasTeam = false;
              if (row) {
                const rowN = norm(row.innerText || '');
                const hN = norm(homeN), aN = norm(awayN);
                if ((hN && hN.length >= 2 && rowN.includes(hN)) || (aN && aN.length >= 2 && rowN.includes(aN))) {
                  rowHasTeam = true;
                }
                if (!rowHasTeam) {
                  for (const tok of (tokens || [])) {
                    const tn2 = norm(tok);
                    if (tn2 && tn2.length >= 3 && rowN.includes(tn2)) { rowHasTeam = true; break; }
                  }
                }
              }
              if (row && !rowHasTeam) {
                return { ok: false, why: 'row_team_mismatch', sample: norm(row.innerText || '').slice(0, 120), how };
              }
              // 无任何行含队名 token：禁止全页兜底（loose/sideloose 会扫全页点到
              // 完全无关行的赔率——实测目标格沃古夫点到巴列卡诺）。fail 让上层
              // 重试搜索/其他 frame。
              if (!row) {
                const bt = norm((document.body && document.body.innerText) || '');
                // 调试：队名 token 命中情况 + 页面样本
                const tokHit = (tokens || []).filter((tk) => {
                  const t2 = norm(tk);
                  return t2 && t2.length >= 2 && bt.includes(t2);
                });
                return {
                  ok: false, why: 'row_not_found',
                  tokHit: tokHit.slice(0, 6),
                  pageTeams: bt.replace(/\\s+/g, ' ').slice(200, 700),
                  how,
                };
              }
              const scope = row;
              if (row) {
                try { row.scrollIntoView({ block: 'center' }); } catch (e) {}
              }
              const clickables = Array.from((scope || document).querySelectorAll('button, a, span, div, td, label'));
              // 行内须含目标盘口线，sideLoose 兜底才允许（防点到相邻盘口同方向的赔率）
              const rowTextN = row ? norm(row.innerText || '') : '';
              const rowHasLineTxt = !lineTxt || lineAliases.some((a) => rowTextN.includes(norm(a)));
              // 让球线预检：行内含负号+小数（如 -0.5/-1.0）时，所有点击路径统一排除
              // 要求小数位避免误同比分（2-1），增加 Unicode 减号/en-dash 覆盖
              // lookbehind (?<!\\d)：dash 前不能是数字，防止四分线 3.5-4 拼赔率 2.130
              // 被误匹配为 -42.13（实盘 odds_not_found 根因）
              const rowHasHandicap = /(?<!\\d)[-－−–]\\d+\\.\\d/.test(rowTextN);
              // side 精确判定：赔率格的「紧邻上下文」须含目标方向词。粗父容器同时含
              // 同一格内可能混有多个方向标签，需检查紧邻上下文。
              // 用相邻兄弟/文本左右字窗口判定，且要求不含反方向词。
              // 小球赔率格必须有紧邻的小球方向词，避免误点相邻盘口。
              // 同时校验市场类型：必须含"大小"/"总分"/"over-under"等总进球市场标签，
              // 且不含"让球"/"让分"/"spread"/"handicap"等让球市场标签，
              // 防止误点让球盘的大/小赔率。
              const spreadWords = ['让球', '让分', 'spread', 'handicap', '盘口', '亚盘'];
              const totalWords = ['大小', '总分', 'over/under', 'over-under', 'o/u', 'totals'];
              const isSpreadCtx = (ctx) => spreadWords.some((w) => String(ctx || '').toLowerCase().includes(w));
              const isTotalCtx = (ctx) => totalWords.some((w) => String(ctx || '').toLowerCase().includes(w));
              const sideNear = (el) => {
                // txt 必须从 el 参数计算：for 循环中的 const txt 是块作用域，
                // sideNear 定义在循环外无法访问 → parentTxt.indexOf(txt) 抛
                // ReferenceError 被静默 catch，导致父容器方向词检查失效
                const txt = String(el.innerText || el.textContent || '').trim();
                let near = '';
                try {
                  let sib = el.previousElementSibling;
                  for (let i = 0; i < 2 && sib; i++) { near += ' ' + String(sib.innerText || ''); sib = sib.previousElementSibling; }
                } catch (e) {}
                try {
                  const parentTxt = String((el.parentElement && el.parentElement.innerText) || '');
                  const idx = parentTxt.indexOf(txt);
                  if (idx >= 0) {
                    near = parentTxt.slice(Math.max(0, idx - 14), idx) + ' ' + (parentTxt.slice(idx + txt.length, idx + txt.length + 6) || '') + near;
                  }
                } catch (e) {}
                // 扩大上下文范围至祖父容器，以便捕获"大小"/"让球"市场标签
                let wideCtx = '';
                try {
                  const grand = el.closest('tr, div[class*="row" i], div[class*="market" i], div[class*="line" i]') || el.parentElement;
                  wideCtx = String((grand && grand.innerText) || near);
                } catch (e) { wideCtx = near; }
                const nl = near.toLowerCase();
                const anti = selDir === 'over' ? ['小'] : ['大'];
                const hit = sideWords.some((w) => nl.includes(String(w).toLowerCase()));
                const hitAnti = anti.some((w) => nl.includes(String(w).toLowerCase()));
                if (hit && !hitAnti) {
                  // 市场类型校验：如果上下文含让球标签则拒绝，含大小标签则放行，
                  // 无明确标签时放行（兼容旧布局）
                  if (isSpreadCtx(wideCtx) && !isTotalCtx(wideCtx)) return false;
                  return true;
                }
                return false;
              };
              for (const el of clickables) {
                const txt = String(el.innerText || el.textContent || '').trim();
                // 清除 U+200E/U+200F 零宽标记，否则纯赔率正则 ^\\d.\\d{2,3}$ 匹配失败
                const pureNum = txt.replace(/[\\s\\u200e\\u200f]/g, '');
                const isPureOdds = /^\\d{1,2}\\.\\d{2,3}$/.test(pureNum);
                if (!oddsTextOk(txt) && !isPureOdds) continue;
                // 纯赔率数字节点（叶子）优先：容器块（\\xa0/换行包裹）点了投注单不开。
                // 漂移护栏 ≤0.15：与 oddsTextOk 阈值一致，防止误点到让球盘的相近赔率
                if (isPureOdds) {
                  const n = Number(pureNum.replace(/[^0-9.]/g, ''));
                  if (n && Math.abs(n - Number(odds)) <= 0.15) {
                    const p = el.closest('div, tr, li, section') || el.parentElement;
                    const ctx = String((p && p.innerText) || txt);
                                        const hitSide = sideNear(el);
                    const hitLine = lineTxt ? hitLineTxt(ctx) : true;
                    const inRow = !!(row && row.contains(el));
                    const rowTxt2 = inRow ? norm(row.innerText || '') : '';
                    const hitSideRow = hitSide || (() => {
                      if (!inRow || !rowTxt2) return false;
                      const elTxt = norm(txt);
                      const idx = rowTxt2.indexOf(elTxt);
                      if (idx < 0) return sideWords.some((w) => rowTxt2.includes(w));
                      for (const w of sideWords) {
                        const wIdx = rowTxt2.indexOf(w);
                        if (wIdx >= 0 && Math.abs(idx - wIdx) <= 30) return true;
                      }
                      return false;
                    })();
                    const hitLineRow = hitLine || (lineTxt && inRow && hitLineTxt(rowTxt2));
                    const rowNotSpread2 = !isSpreadCtx(rowTxt2) || isTotalCtx(rowTxt2);
                    const isLeaf = !(el.children && el.children.length);
                    const leafBetter = !pureOdds || (pureOdds.children && pureOdds.children.length && isLeaf);
                    if (hitSide && hitLine && leafBetter && !rowHasHandicap) { pureOdds = el; }
                    // 行级双命中兜底（嵌套布局兼容，排除让球盘）
                    if (!pureOdds && hitSideRow && hitLineRow && rowNotSpread2 && !rowHasHandicap && leafBetter) { pureOdds = el; }
                    // 漂移兜底：side 命中且线属于本场（行内含线）且非让球盘才收首个
                    if (hitSide && rowHasLineTxt && !rowHasHandicap && !sideLoose) { sideLoose = el; }
                    if (!sideLoose && hitSideRow && rowHasLineTxt && rowNotSpread2 && !rowHasHandicap) { sideLoose = el; }
                  }
                }
                if (!oddsTextOk(txt)) continue;
                let ctx = '';
                try {
                  const p = el.closest('div, tr, li, section') || el.parentElement;
                  ctx = String((p && p.innerText) || txt);
                } catch (e) { ctx = txt; }
                                const hitSide = sideNear(el);
                const hitLine = lineTxt ? hitLineTxt(ctx) : false;
                const inRow = !!(row && row.contains(el));
                // 紧邻上下文未命中时，扩大到祖父级行级上下文重试
                // 平博嵌套布局：大小/让球各自独立 div，closest('div') 只取到子容器
                // 行级校验：在行文本中找 sideWord 和 lineTxt 的位置，判断赔率格是否在二者附近
                const rowTxt = inRow ? norm(row.innerText || '') : '';
                const hitSideRow = hitSide || (() => {
                  if (!inRow || !rowTxt) return false;
                  // 行级 sideNear：检查赔率格在行文本中是否靠近 sideWord（±30字符窗口）
                  const elTxt = norm(txt);
                  const idx = rowTxt.indexOf(elTxt);
                  if (idx < 0) return sideWords.some((w) => rowTxt.includes(w));
                  for (const w of sideWords) {
                    const wIdx = rowTxt.indexOf(w);
                    if (wIdx >= 0 && Math.abs(idx - wIdx) <= 30) return true;
                  }
                  return false;
                })();
                const hitLineRow = hitLine || (lineTxt && inRow && hitLineTxt(rowTxt));
                // 行级命中时额外校验：赔率格附近不能有让球标签（防止点到让球盘的"小"）
                const rowNotSpread = !isSpreadCtx(rowTxt) || isTotalCtx(rowTxt);
                if (!lineTxt) {
                  // 反方向格跳过：under 跳小、over 跳大（无盘口线时的方向词格）
                  const antiWord = selDir === 'over' ? '小' : '大';
                  const ouCell = new RegExp('(?:^|[\\\\s])(' + antiWord + ')(?:$|[\\\\s])', 'i').test(ctx) && !hitSide;
                  if (ouCell) continue;
                  if (hitSide) { target = el; how = (how || 'odds') + '+side'; break; }
                  if (inRow && !rowFallback) { rowFallback = el; }
                  continue;
                }
                // 大小球：方向与盘口线双命中才精确点击。
                // 优先紧邻上下文双命中；未命中时尝试行级双命中（兼容嵌套布局）
                // 所有路径统一校验 rowHasHandicap：行内含让球线时禁止点击
                if (hitSide && hitLine && !rowHasHandicap) { target = el; how = (how || 'odds') + '+side+line'; break; }
                if (hitSideRow && hitLineRow && rowNotSpread && !rowHasHandicap) {
                  target = el; how = (how || 'odds') + '+siderow+linerow'; break;
                }
              }
              if (pureOdds) { target = pureOdds; how = (how || 'odds') + '+pure'; }
              if (!target && sideLoose) { target = sideLoose; how = (how || 'odds') + '+sideloose'; }
              // rowFallback 不校验方向：仅无盘口线信息（独赢）时可用
              if (!target && !lineTxt && rowFallback) { target = rowFallback; how = (how || 'odds') + '+row'; }
              // ── 方向标签缺失兜底：行/线/赔率值三命中但 sideNear 失败 ──
              // 平博中文布局中"大"字可能在独立 header 元素、不包含在行 innerText 里。
              // 此时收集行内所有匹配赔率值（±0.25）的节点，按位置推断方向：
              // over 赔率通常在 under 之前（左/上），取第一个为 over、第二个为 under。
              if (!target && row && lineTxt && rowHasLineTxt && !rowHasHandicap) {
                const rowOddsEls = [];
                for (const el of clickables) {
                  if (!row.contains(el)) continue;
                  const t2 = String(el.innerText || el.textContent || '').trim();
                  const pn = t2.replace(/[\\s\\u200e\\u200f]/g, '');
                  if (!/^\\d{1,2}\\.\\d{2,3}$/.test(pn)) continue;
                  const nv = Number(pn.replace(/[^0-9.]/g, ''));
                  if (nv && Math.abs(nv - Number(odds)) <= 0.25) {
                    rowOddsEls.push({ el, val: nv, idx: rowOddsEls.length });
                  }
                }
                if (rowOddsEls.length >= 1) {
                  const rowN2 = norm(row.innerText || '');
                  const hasUnder = rowN2.includes('小');
                  const hasOver = rowN2.includes('大');
                  // 查找离 fromIdx 最近的 ch 字符位置（同行多盘口时定位各自标签）
                  const findNearest = (ch, fromIdx) => {
                    let best = -1, bestDist = 1e9, sf = 0;
                    while (true) {
                      const idx = rowN2.indexOf(ch, sf);
                      if (idx < 0) break;
                      const d = Math.abs(fromIdx - idx);
                      if (d < bestDist) { bestDist = d; best = idx; }
                      sf = idx + 1;
                    }
                    return best;
                  };
                  // 平博 compact 布局：[线] [over赔率] 小 [under赔率]
                  // 无"大"标签时 over 在"小"之前，under 在"小"之后
                  // 同行多盘口（如 2.5-3 和 1.5）取离各自最近标签的前/后赔率，不跨盘口误选
                  const pickByPos = (label, wantBefore) => {
                    let bestEl = null, bestDist = 1e9;
                    for (const oe of rowOddsEls) {
                      const oeTxt = norm(String(oe.el.innerText || ''));
                      const oeIdx = rowN2.indexOf(oeTxt);
                      if (oeIdx < 0) continue;
                      const labelIdx = findNearest(label, oeIdx);
                      if (labelIdx < 0) continue;
                      const isBefore = oeIdx < labelIdx;
                      if (wantBefore === isBefore) {
                        const dist = Math.abs(oeIdx - labelIdx);
                        if (dist < bestDist) { bestDist = dist; bestEl = oe.el; }
                      }
                    }
                    return bestEl;
                  };
                  if (selDir === 'over' && !hasOver && hasUnder) {
                    // 无"大"标签 → over 赔率在"小"之前，取最近的一个
                    const el = pickByPos('小', true);
                    if (el) { target = el; how = (how || 'odds') + '+noSideLabelOver'; }
                  } else if (selDir === 'under' && !hasUnder && hasOver) {
                    // 无"小"标签 → under 赔率在"大"之后，取最近的一个
                    const el = pickByPos('大', false);
                    if (el) { target = el; how = (how || 'odds') + '+noSideLabelUnder'; }
                  } else if (selDir === 'over' && hasOver) {
                    // 有"大"但 sideNear 失败 → over 在"大"之后
                    const el = pickByPos('大', false);
                    if (el) { target = el; how = (how || 'odds') + '+nearDaOver'; }
                  } else if (selDir === 'under' && hasUnder) {
                    // 有"小"但 sideNear 失败 → under 在"小"之后
                    const el = pickByPos('小', false);
                    if (el) { target = el; how = (how || 'odds') + '+nearXiaoUnder'; }
                  } else if (rowOddsEls.length === 1) {
                    // 行内仅一个匹配赔率 → 直接点击（已通过行/线/赔率值三重校验）
                    target = rowOddsEls[0].el;
                    how = (how || 'odds') + '+singleOddsFallback';
                  }
                }
              }
              // Real compact UI is flat: handicap and totals are sibling cells in one event
              // row, with no dedicated market wrapper.  Resolve from document order:
              // [total line] [over price] [小] [under price].
              const flatLeaves = Array.from(row.querySelectorAll('*')).filter((el) => {
                if (el.children && el.children.length) return false;
                const t = String(el.innerText || el.textContent || '').replace(/[\\s\\u200e\\u200f]/g, '');
                return !!t && t.length <= 30;
              });
              const flatText = (el) => String(el.innerText || el.textContent || '').replace(/[\\s\\u200e\\u200f]/g, '').trim();
              const flatPrice = (el) => {
                const t = flatText(el);
                if (!/^\\d{1,2}\\.\\d{2,3}$/.test(t)) return null;
                const n = Number(t);
                return n >= 1.01 && n <= 50 ? n : null;
              };
              const exactSideLabel = (text, direction) => {
                const t = norm(text);
                if (direction === 'under') return t === '小' || t === 'under' || t === '低于';
                return t === '大' || t === 'over' || t === '高于';
              };
              const flatCandidates = [];
              const seenFlat = new Set();
              for (let i = 0; i < flatLeaves.length; i++) {
                const lineEl = flatLeaves[i];
                if (!hitLineTxt(flatText(lineEl))) continue;
                const windowEls = flatLeaves.slice(i + 1, i + 12);
                let priceEl = null;
                if (selDir === 'under') {
                  const sideAt = windowEls.findIndex((el) => exactSideLabel(flatText(el), 'under'));
                  if (sideAt >= 0) {
                    priceEl = windowEls.slice(sideAt + 1).find((el) => flatPrice(el) != null) || null;
                  }
                } else {
                  const overAt = windowEls.findIndex((el) => exactSideLabel(flatText(el), 'over'));
                  if (overAt >= 0) {
                    priceEl = windowEls.slice(overAt + 1).find((el) => flatPrice(el) != null) || null;
                  } else {
                    // Chinese compact omits 大.  The only price between the line and 小 is over.
                    const underAt = windowEls.findIndex((el) => exactSideLabel(flatText(el), 'under'));
                    if (underAt > 0) {
                      const beforeUnder = windowEls.slice(0, underAt).filter((el) => flatPrice(el) != null);
                      if (beforeUnder.length === 1) priceEl = beforeUnder[0];
                    }
                  }
                }
                if (!priceEl) continue;
                const clickEl = priceEl.closest('button, a, [role="button"]') || priceEl;
                if (seenFlat.has(clickEl)) continue;
                seenFlat.add(clickEl);
                flatCandidates.push({
                  clickEl,
                  priceEl,
                  price: flatPrice(priceEl),
                  distance: Math.abs(Number(flatPrice(priceEl)) - Number(odds)),
                  line: flatText(lineEl),
                });
              }
              flatCandidates.sort((a, b) => a.distance - b.distance);
              const flatResolved = flatCandidates.length === 1;
              if (flatResolved) {
                target = flatCandidates[0].clickEl;
                how = (how || 'odds') + '+flatExactLineDirection';
              } else if (flatCandidates.length > 1) {
                return { ok: false, why: 'ambiguous_flat_total', sample: JSON.stringify(flatCandidates.slice(0, 3).map((x) => ({line:x.line,price:x.price}))), how };
              }
              if (!target) {
                const body = norm(document.body && document.body.innerText || '');
                const idx = row ? norm(row.innerText || '').slice(0, 180) : body.slice(0, 180);
                return { ok: false, why: row ? 'odds_not_found' : 'row_not_found', sample: idx, how, odds };
              }
              // Final click guard: the old search above may inspect an entire event row where
              // moneyline, handicap and total prices coexist.  Before clicking, independently
              // prove that the chosen price belongs to one small local total-market container.
              const strictSpreadWords = ['让球', '让分', 'spread', 'handicap', '亚盘'];
              const strictPurePrice = (el) => {
                const t = String(el.innerText || el.textContent || '').replace(/[\\s\\u200e\\u200f]/g, '');
                if (!/^\\d{1,2}\\.\\d{2,3}$/.test(t)) return null;
                const n = Number(t);
                return n >= 1.01 && n <= 50 ? n : null;
              };
              const strictLineNode = (box) => {
                const ns = [box, ...Array.from(box.querySelectorAll('*'))];
                return ns.some((n) => {
                  const t = String(n.innerText || n.textContent || '').trim();
                  return t.length <= 40 && hitLineTxt(t);
                });
              };
              const strictFlatten = (box, priceEl) => {
                let text = '', priceAt = -1;
                const walker = document.createTreeWalker(box, NodeFilter.SHOW_TEXT);
                let node;
                while ((node = walker.nextNode())) {
                  const chunk = norm(node.nodeValue || '');
                  if (!chunk) continue;
                  if (priceAt < 0 && priceEl.contains(node.parentElement)) priceAt = text.length;
                  text += chunk;
                }
                return { text, priceAt };
              };
              const strictLabelPositions = (text, label) => {
                const out = [];
                let at = 0;
                while ((at = text.indexOf(label, at)) >= 0) {
                  // Ignore the market heading 大小; it is not a direction selection.
                  if (!(label === '大' && text.slice(at, at + 2) === '大小')
                      && !(label === '小' && text.slice(Math.max(0, at - 1), at + 1) === '大小')) out.push(at);
                  at += label.length;
                }
                return out;
              };
              const strictDirection = (box, priceEl) => {
                const flat = strictFlatten(box, priceEl);
                if (flat.priceAt < 0) return false;
                const desired = selDir === 'over' ? ['大', 'over', '高于'] : ['小', 'under', '低于'];
                const opposite = selDir === 'over' ? ['小', 'under', '低于'] : ['大', 'over', '高于'];
                const wanted = desired.flatMap((w) => strictLabelPositions(flat.text, norm(w)));
                const anti = opposite.flatMap((w) => strictLabelPositions(flat.text, norm(w)));
                const nearestBefore = (positions) => positions
                  .filter((p) => p <= flat.priceAt && flat.priceAt - p <= 18)
                  .reduce((best, p) => Math.max(best, p), -1);
                const wantedBefore = nearestBefore(wanted);
                const antiBefore = nearestBefore(anti);
                // When both 大 and 小 precede the second price, the nearer label wins.
                if (wantedBefore >= 0 && wantedBefore > antiBefore) return true;
                // Compact layout omits 大: [line][over price] 小[under price].
                if (selDir === 'over' && !wanted.length) {
                  return anti.some((p) => p > flat.priceAt && p - flat.priceAt <= 18);
                }
                return false;
              };
              // Re-scan from local market containers; do not trust the candidate selected by
              // the broad legacy pass above.  One total market has exactly one/two price leaves.
              const strictCandidates = [];
              const seenStrict = new Set();
              for (const priceEl of Array.from(row.querySelectorAll('*'))) {
                if (priceEl.children && priceEl.children.length) continue;
                const price = strictPurePrice(priceEl);
                if (price == null || Math.abs(price - Number(odds)) > 0.15) continue;
                if (hitLineTxt(String(priceEl.innerText || priceEl.textContent || ''))) continue;
                for (let box = priceEl.parentElement, depth = 0;
                     box && depth < 8 && row.contains(box);
                     box = box.parentElement, depth++) {
                  const raw = String(box.innerText || '');
                  if (!raw || raw.length > 180 || !strictLineNode(box)) continue;
                  const prices = Array.from(box.querySelectorAll('*'))
                    .filter((n) => !(n.children && n.children.length) && strictPurePrice(n) != null);
                  if (prices.length < 1 || prices.length > 2) continue;
                  if (strictSpreadWords.some((w) => raw.toLowerCase().includes(w))) continue;
                  if (/(主队总|客队总|球队总|team\\s*total|角球|corners|罚牌|黄牌|红牌|cards|球员|player)/i.test(raw)) continue;
                  if (/(?<!\\d)[-－−–]\\d+\\.\\d/.test(norm(raw))) continue;
                  if (!strictDirection(box, priceEl)) continue;
                  const clickEl = priceEl.closest('button, a, [role="button"]') || priceEl;
                  if (seenStrict.has(clickEl)) break;
                  seenStrict.add(clickEl);
                  const distance = Math.abs(price - Number(odds));
                  strictCandidates.push({ clickEl, priceEl, box, price, distance, raw });
                  break;
                }
              }
              strictCandidates.sort((a, b) => a.distance - b.distance || a.raw.length - b.raw.length);
              // Real compact board may render one league as a CSS grid: team cells and
              // market cells are siblings, so there is no small DOM ancestor containing
              // both the line and its two prices.  In that layout the broad `row` is
              // unavoidable.  Resolve the market from text-node order instead:
              //   teams ... handicap ... [total line] [over price] 小 [under price]
              // The target total must be close to the target team's text nodes and have
              // exactly one price before 小.  A handicap cell has no 小 marker, therefore
              // it cannot pass this fallback.
              if (!strictCandidates.length && !flatResolved) {
                const textItems = [];
                try {
                  const walker = document.createTreeWalker(row, NodeFilter.SHOW_TEXT);
                  let node;
                  while ((node = walker.nextNode())) {
                    const raw = String(node.nodeValue || '').trim();
                    const t = norm(raw);
                    const el = node.parentElement;
                    if (!t || !el) continue;
                    let visible = true;
                    try {
                      const st = getComputedStyle(el), r = el.getBoundingClientRect();
                      visible = st.display !== 'none' && st.visibility !== 'hidden'
                        && st.opacity !== '0' && r.width > 0 && r.height > 0;
                    } catch (e) {}
                    if (visible) textItems.push({ raw, text: t, el });
                  }
                } catch (e) {}
                const fullTeams = [norm(homeN), norm(awayN)].filter((x) => x && x.length >= 2);
                const strongTokens = (tokens || []).map(norm)
                  .filter((x) => x && x.length >= 3)
                  .sort((a, b) => b.length - a.length);
                const teamIndexes = [];
                for (let i = 0; i < textItems.length; i++) {
                  const t = textItems[i].text;
                  if (fullTeams.some((x) => t.includes(x) || x.includes(t))
                      || strongTokens.slice(0, 4).some((x) => t.includes(x))) {
                    teamIndexes.push(i);
                  }
                }
                const sequenceCandidates = [];
                for (let i = 0; i < textItems.length; i++) {
                  const lineItem = textItems[i];
                  if (lineItem.raw.length > 30 || !hitLineTxt(lineItem.raw)) continue;
                  const precedingTeams = teamIndexes.filter((p) => p <= i);
                  if (!precedingTeams.length) continue;
                  const teamDistance = i - Math.max(...precedingTeams);
                  // One event occupies roughly 10-25 text cells.  A farther line belongs
                  // to another event in the same league grid and must not be considered.
                  if (teamDistance < 1 || teamDistance > 32) continue;
                  const windowEnd = Math.min(textItems.length, i + 15);
                  let underAt = -1;
                  for (let j = i + 1; j < windowEnd; j++) {
                    if (exactSideLabel(textItems[j].raw, 'under')) { underAt = j; break; }
                  }
                  if (underAt < 0) continue;
                  const priceItems = (from, to) => textItems.slice(from, to)
                    .map((item, offset) => ({ item, index: from + offset, price: strictPurePrice(item.el) }))
                    .filter((x) => x.price != null);
                  const beforeUnder = priceItems(i + 1, underAt);
                  // The standard total is the only compact cell shaped
                  // [line][one over price]小[under price].
                  if (beforeUnder.length !== 1) continue;
                  const afterUnder = priceItems(underAt + 1, Math.min(windowEnd, underAt + 5));
                  if (!afterUnder.length) continue;
                  const picked = selDir === 'over' ? beforeUnder[0] : afterUnder[0];
                  if (Math.abs(picked.price - Number(odds)) > 0.15) continue;
                  const localText = textItems.slice(Math.max(0, i - 3), Math.min(windowEnd, underAt + 5))
                    .map((x) => x.raw).join(' ');
                  if (/(主队总|客队总|球队总|team\\s*total|角球|corners|罚牌|黄牌|红牌|cards|球员|player)/i.test(localText)) continue;
                  const clickEl = picked.item.el.closest('button, a, [role="button"]') || picked.item.el;
                  sequenceCandidates.push({
                    clickEl,
                    priceEl: picked.item.el,
                    box: row,
                    price: picked.price,
                    distance: Math.abs(picked.price - Number(odds)),
                    teamDistance,
                    raw: `textNodeTotal:${lineItem.raw}:${picked.price}`,
                  });
                }
                sequenceCandidates.sort((a, b) =>
                  a.teamDistance - b.teamDistance || a.distance - b.distance
                );
                if (sequenceCandidates.length === 1
                    || (sequenceCandidates.length > 1
                        && sequenceCandidates[0].teamDistance < sequenceCandidates[1].teamDistance)) {
                  strictCandidates.push(sequenceCandidates[0]);
                  how = (how || 'odds') + '+textNodeTotal';
                } else if (sequenceCandidates.length > 1) {
                  return {
                    ok: false,
                    why: 'ambiguous_text_total',
                    sample: sequenceCandidates.slice(0, 3).map((c) => c.raw).join(' || '),
                    how,
                  };
                }
              }
              if (flatResolved && !seenStrict.has(target)) {
                seenStrict.add(target);
                strictCandidates.push({
                  clickEl: target,
                  priceEl: flatCandidates[0].priceEl,
                  box: row,
                  price: flatCandidates[0].price,
                  distance: flatCandidates[0].distance,
                  raw: `flat:${flatCandidates[0].line}`,
                });
              }
              if (!strictCandidates.length) {
                return { ok: false, why: 'unsafe_total_context', sample: String(row.innerText || '').slice(0, 160), how };
              }
              if (strictCandidates.length > 1
                  && Math.abs(strictCandidates[0].distance - strictCandidates[1].distance) < 0.000001) {
                return { ok: false, why: 'ambiguous_total_context', sample: strictCandidates.slice(0, 2).map((c) => c.raw).join(' || '), how };
              }
              target = strictCandidates[0].clickEl;
              how = (how || 'odds') + '+strictLocalTotal';
              try { target.scrollIntoView({ block: 'center' }); } catch (e) {}
              try { target.click(); } catch (e) {
                try { target.dispatchEvent(new MouseEvent('click', { bubbles: true })); } catch (e2) {}
              }
              return { ok: true, why: 'clicked', sample: String(target.innerText || '').slice(0, 40), how };
            }"""
    args = {
        "tokens": tokens[:24],
        "odds": float(odds),
        "sideWords": side_words,
        "line": float(line) if line is not None else None,
        "homeN": (home or "").strip(),
        "awayN": (away or "").strip(),
        "selDir": sel,
        "eventId": re.sub(r"\D", "", str(event_id or ""))[:24],
    }

    # SPA 惰性激活：compact/sports 页可能只渲染头部导航（大厅壳），赛事区需
    # 用户交互才加载。但若已在 /live 或 /compact/sports/ 页，绝不点「体育」标签
    # （会导航到大厅，打掉已渲染的滚球列表 -> len=0 -> ui_miss）。
    try:
        async def _body_has_odds() -> bool:
            try:
                t = await page.evaluate(
                    "() => ((document.body && document.body.innerText) || '')"
                )
            except Exception:
                return True
            import re as _re

            return bool(_re.search(r"(?<![0-9])1\.\d{2,3}(?![0-9])", t or ""))

        if not await _body_has_odds():
            cur_url_spa = ""
            try:
                cur_url_spa = (page.url or "").lower()
            except Exception:
                cur_url_spa = ""
            on_sports_page = "/live" in cur_url_spa or "/compact/sports/" in cur_url_spa
            logger.info(
                "pinnacle ui: body no odds, on_sports_page=%s url=%s",
                on_sports_page,
                cur_url_spa[:100],
            )

            if on_sports_page:
                # 轮询等 SPA 渲染（最多 18s）
                for _ in range(12):
                    if await _body_has_odds():
                        break
                    await page.wait_for_timeout(1500)
                # 仍无赔率：滚一下触发懒加载
                if not await _body_has_odds():
                    try:
                        await page.evaluate(
                            "() => window.scrollTo(0, document.body.scrollHeight / 2)"
                        )
                        await page.wait_for_timeout(2500)
                    except Exception:
                        pass
            else:
                return False, "not_on_sports_page", Decimal("0"), ""
    except Exception as e:
        logger.warning("pinnacle ui: sportsbook activate failed: %s", e)

    async def _try_click_all_frames() -> tuple[dict | None, str]:
        try:
            await page.wait_for_timeout(800)
        except Exception:
            pass
        targets = []
        try:
            targets = list(page.frames or [])
        except Exception:
            targets = []
        if not targets:
            targets = [page]
        ordered = []
        frame_dbg = []
        for fr in targets:
            try:
                fu = fr.url or ""
            except Exception:
                fu = ""
            ful = fu.lower()
            if ful.startswith("about:") or ful == "":
                continue
            score = 0
            if any(x in ful for x in ("sport", "soccer", "football", "basket", "live", "compact", "match")):
                score += 3
            if fr == getattr(page, "main_frame", None):
                score += 1
            try:
                meta = await fr.evaluate(
                    """() => {
                      const t = (document.body && (document.body.innerText || '')) || '';
                      return {
                        len: t.length,
                        hello: t.trim().toLowerCase() === 'hello',
                        slip: t.includes('投注单') || t.includes('投下') || t.includes('确认投注'),
                        live: t.includes('滚球') || t.includes('In-Play') || t.includes('独赢'),
                      };
                    }"""
                )
            except Exception:
                meta = {"len": 0, "hello": True, "slip": False, "live": False}
            blen = int((meta or {}).get("len") or 0)
            is_main = fr == getattr(page, "main_frame", None)
            if (meta or {}).get("hello") or (blen < 40 and not is_main):
                score -= 20
            else:
                score += min(4, blen // 200)
            if is_main:
                score = max(score, 0)  # 主帧不因空内容被跳过（SPA 可能还在渲染）
            if (meta or {}).get("slip"):
                score += 8
            if (meta or {}).get("live"):
                score += 3
            frame_dbg.append(f"{score}:{fu[:80]}:len={blen}:slip={bool((meta or {}).get('slip'))}")
            ordered.append((score, fr))
        ordered.sort(key=lambda x: -x[0])
        logger.info("pinnacle ui frames %s", " | ".join(frame_dbg[:8]))
        miss = "ui_miss"
        if not ordered:
            return None, "no_frames"

        # 滚球列表渲染等待：按「内容特征」判定而非字符数
        # （实测 len=1632 骨架半渲染态：有导航/页脚但赛事行未出，字符数阈值会漏）
        # 判定标准：页面含 ≥2 个滚球行特征（比分 X-X + 分钟 ' / 节次 Q1-Q4/1H2H）
        # 或含 ≥3 个赔率数字（1.xx-9.xx），赔率存在即可点
        async def _wait_board_ready() -> None:
            try:
                probe = """() => {
                  const t = ((document.body && document.body.innerText) || '');
                  // 比分格式：数字-数字（如 2-1），排除四分线拼赔率（如 3.5-42.130 中的 5-42）
                  const scores = (t.match(/(?<![0-9.])\\d{1,2}-\\d{1,2}(?![0-9.])/g) || []).length;
                  const clocks = (t.match(/(?:^|\\s)(?:\\d{1,3}'|1H|2H|HT|Q[1-4]|P[1-4])(?:$|\\s)/gm) || []).length;
                  const odds = (t.match(/(?<![0-9])[1-9]\\.\\d{2,3}(?![0-9])/g) || []).length;
                  return scores + clocks + (odds >= 3 ? 10 : 0);
                }"""
                for _ in range(15):
                    try:
                        feats = int(await page.evaluate(probe))
                    except Exception:
                        feats = 0
                    if feats >= 4:
                        return
                    await page.wait_for_timeout(1000)
            except Exception:
                pass

        await _wait_board_ready()

        for score, fr in ordered:
            if score < -10:
                continue
            try:
                data = await asyncio.wait_for(fr.evaluate(click_js, args), timeout=8.0)
            except Exception as e:
                miss = f"evaluate_fail:{e}"
                continue
            if isinstance(data, dict) and data.get("ok"):
                return data, "ok"
            if isinstance(data, dict):
                why = data.get("why", "")
                if why == "row_not_found":
                    tok_hit = data.get("tokHit") or []
                    page_teams = str(data.get("pageTeams") or "")[:200]
                    logger.warning(
                        "pinnacle ui row_not_found tokHit=%s pageTeams=%s",
                        tok_hit,
                        page_teams,
                    )
                    miss = f"row_not_found:tokHit={tok_hit}"
                else:
                    # Keep enough of the single event row to diagnose rapidly moving
                    # basketball total lines; still bounded and contains no credentials.
                    miss = f"{why}:{(data.get('sample') or '')[:300]}"
        return None, miss

    # 上一次遗留的确认弹窗绝不能点 OK，否则会提交上一单。只在弹窗内部点取消。
    cancel_stale_confirm_js = """() => {
      let modal = null;
      for (const el of document.querySelectorAll('[role="dialog"], [class*="modal" i], [class*="dialog" i], div, section')) {
        const text = String(el.innerText || '');
        if (!text.includes('您是否想要投注') && !(text.includes('确认投注') && text.includes('取消'))) continue;
        if (!modal || text.length < String(modal.innerText || '').length) modal = el;
      }
      if (!modal) return { ok: false, why: 'no_modal' };
      for (const el of modal.querySelectorAll('button, a, div[role="button"], input')) {
        const t = String(el.innerText || el.textContent || el.value || '').replace(/\\s+/g, ' ').trim();
        if (t === '取消' || /^cancel$/i.test(t)) {
          try { el.click(); return { ok: true, text: t }; } catch (e) {}
        }
      }
      return { ok: false, why: 'cancel_missing' };
    }"""
    try:
        for fr in ([page] + list(getattr(page, "frames", []) or [])):
            try:
                resumed = await asyncio.wait_for(
                    fr.evaluate(cancel_stale_confirm_js), timeout=3.0
                )
            except Exception:
                continue
            if isinstance(resumed, dict) and resumed.get("ok"):
                await page.wait_for_timeout(700)
                logger.info("pinnacle stale confirmation cancelled; continue cleanup")
                break
    except Exception:
        pass
    try:
        if await page.get_by_text("您是否想要投注", exact=False).count() > 0:
            cancel_btn = page.get_by_role("button", name=re.compile(r"取消|Cancel", re.I)).first
            if await cancel_btn.count() > 0:
                await cancel_btn.click(timeout=2500)
                await page.wait_for_timeout(700)
                logger.info("pinnacle stale confirmation cancelled by locator")
    except Exception:
        pass

    # 上方已经按目标球类做过一次确定性跳转；这里禁止再点球类 Tab。
    # 但如果跳转后 URL 仍不匹配（SPA 异步路由/goto 失败），再尝试一次而非直接拒绝。
    try:
        want_fb = "basket" not in sport_l
        cur_u = (page.url or "").lower()
        need_sport = (
            (want_fb and "soccer" not in cur_u and "football" not in cur_u)
            or ((not want_fb) and "basket" not in cur_u)
        )
        if need_sport:
            # 二次跳转兜底：上方 goto 可能因 SPA 异步路由未生效
            wanted_sport = "basketball" if not want_fb else "soccer"
            from urllib.parse import urlparse as _pu3
            _p3 = _pu3(page.url or "")
            _org3 = f"{_p3.scheme}://{_p3.netloc}" if _p3.netloc else ""
            if _org3:
                try:
                    await page.goto(
                        f"{_org3}/zh-cn/compact/sports/{wanted_sport}{'/live' if live_only else ''}",
                        wait_until="domcontentloaded",
                        timeout=30000,
                    )
                    await page.wait_for_timeout(3000)
                    cur_u = (page.url or "").lower()
                except Exception as e:
                    logger.warning("pinnacle ui: sport retry goto err: %s", e)
            # 再次检查
            need_sport = (
                (want_fb and "soccer" not in cur_u and "football" not in cur_u)
                or ((not want_fb) and "basket" not in cur_u)
            )
            if need_sport or "ice" in cur_u or "hockey" in cur_u:
                return False, "wrong_sport_page", Decimal("0"), ""
    except Exception as e:
        logger.debug("pinnacle sport tab ensure: %s", e)

    # 搜索队名，把目标赛事滚到可见（平博 compact 顶部搜索框）
    async def _search_team(q: str) -> bool:
        q = (q or "").strip()
        if len(q) < 2:
            return False
        search_js = """(q) => {
          const inputs = Array.from(document.querySelectorAll('input'));
          let hit = null;
          for (const inp of inputs) {
            const ph = ((inp.getAttribute('placeholder') || '') + ' ' + (inp.getAttribute('aria-label') || '')).toLowerCase();
            if (/搜索|search|联赛|队伍|队伍名称/.test(ph)) { hit = inp; break; }
          }
          if (!hit) {
            for (const inp of inputs) {
              try {
                const st = window.getComputedStyle(inp);
                if (st.display === 'none' || st.visibility === 'hidden') continue;
                const r = inp.getBoundingClientRect();
                if (r.top < 120 && r.width > 120) { hit = inp; break; }
              } catch (e) {}
            }
          }
          if (!hit) return { ok: false };
          try { hit.focus(); hit.click(); hit.select && hit.select(); } catch (e) {}
          const proto = window.HTMLInputElement && window.HTMLInputElement.prototype;
          const desc = proto && Object.getOwnPropertyDescriptor(proto, 'value');
          if (desc && desc.set) desc.set.call(hit, q); else hit.value = q;
          for (const ev of ['input', 'change', 'keyup']) {
            try { hit.dispatchEvent(new Event(ev, { bubbles: true })); } catch (e) {}
          }
          return { ok: true };
        }"""
        try:
            targets = [page] + [fr for fr in (page.frames or []) if fr != getattr(page, "main_frame", None)]
        except Exception:
            targets = [page]
        for fr in targets:
            try:
                data = await asyncio.wait_for(fr.evaluate(search_js, q), timeout=4.0)
            except Exception:
                continue
            if isinstance(data, dict) and data.get("ok"):
                # 禁止 Enter：平博会跳到 /compact/search/... 打掉滚球列表
                await page.wait_for_timeout(900)
                return True
        try:
            box = page.get_by_placeholder(re.compile(r"搜索|Search")).first
            if await box.count() > 0:
                await box.click(timeout=2000)
                await box.fill(q)
                await page.wait_for_timeout(900)
                return True
        except Exception:
            pass
        return False

    async def _clear_team_search() -> bool:
        """清除 compact 列表过滤；受控输入必须派发 input/change。"""
        clear_js = """() => {
          let cleared = false;
          for (const inp of document.querySelectorAll('input')) {
            const ph = ((inp.getAttribute('placeholder') || '') + ' '
              + (inp.getAttribute('aria-label') || '')).toLowerCase();
            if (!/搜索|search|联赛|队伍|队伍名称/.test(ph)) continue;
            if (!inp.value) continue;
            try { inp.focus(); } catch (e) {}
            const proto = window.HTMLInputElement && window.HTMLInputElement.prototype;
            const desc = proto && Object.getOwnPropertyDescriptor(proto, 'value');
            if (desc && desc.set) desc.set.call(inp, ''); else inp.value = '';
            for (const ev of ['input', 'change', 'keyup']) {
              try { inp.dispatchEvent(new Event(ev, { bubbles: true })); } catch (e) {}
            }
            cleared = true;
          }
          return cleared;
        }"""
        changed = False
        try:
            frames = [page] + [
                fr for fr in (page.frames or [])
                if fr != getattr(page, "main_frame", None)
            ]
        except Exception:
            frames = [page]
        for fr in frames:
            try:
                changed = bool(
                    await asyncio.wait_for(fr.evaluate(clear_js), timeout=3.0)
                ) or changed
            except Exception:
                continue
        if changed:
            await page.wait_for_timeout(700)
        return changed

    # 必须留在目标体育列表；后台/下单均不得把平博带到其它路由。
    try:
        cur = (page.url or "").lower()
        wrong_live_scope = (live_only and "/live" not in cur) or (
            (not live_only) and "/live" in cur
        )
        if "/search" in cur or "/compact/sports/" not in cur or wrong_live_scope:
            return False, "not_on_live_sports_page", Decimal("0"), ""
    except Exception:
        return False, "sports_page_unavailable", Decimal("0"), ""
    # 每次自动下单都从空投注单开始。若有上次失败残留，严格执行
    # 「清除全部/移除全部 -> 好的」，并验证投注单已空。
    cleaned, clean_detail = await cleanup_pinnacle_failed_slips(page)
    if not cleaned:
        return False, f"initial_slip_cleanup_failed|{clean_detail}", Decimal("0"), ""

    async def _cleanup_slip_on_failure() -> None:
        """下单失败后清空所有残留，并记录是否确实清空。"""
        ok, detail = await cleanup_pinnacle_failed_slips(page)
        if not ok:
            logger.error("pinnacle failed-slip cleanup failed: %s", detail)

    # 先清理历史过滤，然后直接用 mid/原生队名扫描完整列表。旧逻辑在首次扫描前
    # 输入中文队名，会把原生译名不同的赛事全部过滤掉，制造 tokHit=[]。
    await _clear_team_search()
    result = None
    last_miss = "ui_miss"
    result, last_miss = await _try_click_all_frames()

    # 仅在未找到赛事行时逐个尝试列表内搜索。每个查询前后都清空过滤，
    # 不把一次零结果带入下一次恢复；绝不按 Enter 跳到 /search。
    if not (isinstance(result, dict) and result.get("ok")) and (
        "row_not_found" in last_miss or last_miss in {"ui_miss", "no_frames"}
    ):
        queries: list[str] = []
        for name in (*alias_names, home, away):
            clean_name = str(name or "").strip()
            if len(clean_name) < 2:
                continue
            for query in (clean_name, clean_name[:8]):
                if len(query) >= 2 and query not in queries:
                    queries.append(query)
        for query in queries[:6]:
            await _clear_team_search()
            if not await _search_team(query):
                continue
            result, last_miss = await _try_click_all_frames()
            if isinstance(result, dict) and result.get("ok"):
                break
        if not (isinstance(result, dict) and result.get("ok")):
            await _clear_team_search()

    # 最后一次安全恢复：只对“页面/赛事行未加载”重新进入同一滚球路由，
    # 不对盘口不匹配、方向不明确等安全拒绝做盲目点击重试。
    if not (isinstance(result, dict) and result.get("ok")) and (
        "row_not_found" in last_miss or last_miss in {"ui_miss", "no_frames"}
    ):
        try:
            current_url = str(page.url or "")
            expected_scope = "/live" in current_url.lower() if live_only else "/live" not in current_url.lower()
            if "/compact/sports/" in current_url.lower() and expected_scope:
                await page.goto(current_url, wait_until="domcontentloaded", timeout=35000)
                await page.wait_for_timeout(3500)
                await dismiss_pinnacle_blocking_modals(page)
                await _clear_team_search()
                result, last_miss = await _try_click_all_frames()
        except Exception as e:
            last_miss = f"board_reload_failed:{type(e).__name__}"

    if not (isinstance(result, dict) and result.get("ok")):
        # Fail closed.  The former Playwright locator fallback clicked the first
        # same-looking price in the whole event row and could bypass the local
        # total-market guard above.
        return False, last_miss or "strict_total_cell_not_found", Decimal("0"), ""

    await page.wait_for_timeout(900)

    # 点赔率后必须出现投注单；否则回滚球列表再试一次点选
    async def _slip_ready() -> bool:
        # 投注单打开：投下N注 / 总注金 / 最低投注额 / 可赢 + 金额输入
        check = """() => {
          const t = String((document.body && document.body.innerText) || '');
          if (/投下\\s*\\d+\\s*注/.test(t) || /总注金\\s*[0-9.]+\\s*CNY/i.test(t)) return true;
          if (/投注单/.test(t) && (/最低投注|最高投注|可赢|潜在返还|Place Bet|Stake/i.test(t))) return true;
          const inputs = Array.from(document.querySelectorAll('input'));
          for (const inp of inputs) {
            try {
              const st = window.getComputedStyle(inp);
              if (!st || st.display === 'none' || st.visibility === 'hidden') continue;
              const ph = ((inp.getAttribute('placeholder') || '') + ' ' + (inp.getAttribute('aria-label') || '')).toLowerCase();
              const ty = (inp.getAttribute('type') || '').toLowerCase();
              if (ty === 'number' || /金额|投注|stake|amount|注金/.test(ph)) {
                const r = inp.getBoundingClientRect();
                if (r.width > 20 && r.height > 10) return true;
              }
            } catch (e) {}
          }
          return false;
        }"""
        try:
            for fr in ([page] + list(getattr(page, "frames", []) or [])):
                try:
                    if await asyncio.wait_for(fr.evaluate(check), timeout=2.0):
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    if not await _slip_ready():
        # 再等一轮：部分 compact 布局点赔率后延迟出单
        for _ in range(4):
            await page.wait_for_timeout(500)
            if await _slip_ready():
                break
    if not await _slip_ready():
        logger.warning("pinnacle slip not open after odds click; retry in place")
        # 清搜索框，避免停在搜索结果页
        try:
            await page.evaluate(
                """() => {
                  const inputs = Array.from(document.querySelectorAll('input'));
                  for (const inp of inputs) {
                    const ph = ((inp.getAttribute('placeholder') || '') + ' ' + (inp.getAttribute('aria-label') || '')).toLowerCase();
                    if (/搜索|search/.test(ph)) { inp.value = ''; inp.dispatchEvent(new Event('input', {bubbles:true})); }
                  }
                }"""
            )
        except Exception:
            pass
        result, last_miss = await _try_click_all_frames()
        if not (isinstance(result, dict) and result.get("ok")):
            await _cleanup_slip_on_failure()
            return False, f"slip_not_open|{last_miss}", Decimal("0"), ""
        await page.wait_for_timeout(1200)
        for _ in range(5):
            if await _slip_ready():
                break
            await page.wait_for_timeout(400)
        if not await _slip_ready():
            await _cleanup_slip_on_failure()
            return False, f"slip_not_open_after_retry|clicked={result.get('sample')}|{result.get('how')}", Decimal("0"), ""

    # Extract the smallest visible bet-slip container anchored by the real
    # "投下 N 注" button and a stake input.  Never validate against body text:
    # body also contains the intended total row and could hide a wrong handicap
    # selection already sitting in the slip.
    slip_snapshot_js = r"""() => {
      const visible = (el) => {
        try {
          const st = window.getComputedStyle(el);
          const r = el.getBoundingClientRect();
          return st && st.display !== 'none' && st.visibility !== 'hidden'
            && st.opacity !== '0' && r.width > 20 && r.height > 15;
        } catch (e) { return false; }
      };
      const textOf = (el) => String(el.innerText || el.textContent || '').replace(/\u200e|\u200f/g, '').trim();
      const hasStakeInput = (el) => Array.from(el.querySelectorAll('input, textarea, [contenteditable="true"]')).some(visible);
      const candidates = [];
      const seen = new Set();
      const add = (el, anchored) => {
        if (!el || seen.has(el) || !visible(el)) return;
        seen.add(el);
        const text = textOf(el);
        if (text.length < 20 || text.length > 1800) return;
        const place = text.match(/投下\s*(\d+)\s*注/i);
        const markers = /投注单|总注金|最低投注|最高投注|可赢|潜在奖金|place\s*bet|stake/i.test(text);
        if (!place && !markers) return;
        if (!hasStakeInput(el)) return;
        const cls = String(el.className || '').toLowerCase();
        let score = anchored ? 120 : 0;
        if (/bet.?slip|betslip|coupon|ticket|wager/.test(cls)) score += 80;
        if (place) score += 50;
        if (/总注金|最低投注|最高投注/.test(text)) score += 30;
        score -= text.length / 100;
        candidates.push({ text, count: place ? Number(place[1]) : null, score });
      };
      for (const el of document.querySelectorAll('button, a, [role="button"], input[type="submit"]')) {
        const label = textOf(el);
        if (!/^投下\s*\d+\s*注$/i.test(label) && !/^place\s*\d+\s*bet$/i.test(label) && label !== 'Place Bet') continue;
        let root = el.parentElement;
        for (let i = 0; root && i < 9; i++, root = root.parentElement) add(root, true);
      }
      for (const el of document.querySelectorAll(
        '[class*="betslip" i], [class*="bet-slip" i], [class*="coupon" i], [class*="wager" i], [data-testid*="betslip" i], aside'
      )) add(el, false);
      candidates.sort((a, b) => b.score - a.score);
      return candidates[0] || null;
    }"""

    async def _validate_current_slip() -> tuple[bool, str]:
        failures: list[str] = []
        try:
            frames = [page] + [
                fr
                for fr in (page.frames or [])
                if fr != getattr(page, "main_frame", None)
            ]
        except Exception:
            frames = [page]
        for fr in frames:
            try:
                snap = await asyncio.wait_for(
                    fr.evaluate(slip_snapshot_js), timeout=3.0
                )
            except Exception:
                continue
            if not isinstance(snap, dict) or not snap.get("text"):
                continue
            ok, reason = _validate_pinnacle_slip_text(
                str(snap.get("text") or ""),
                home=home,
                away=away,
                selection=sel,
                line=line,
                bet_count=snap.get("count"),
                team_aliases=alias_names,
            )
            if ok:
                return True, "ok"
            failures.append(f"{reason}:{str(snap.get('text') or '')[:120]}")
        if failures:
            return False, failures[0]
        return False, "slip_snapshot_missing"

    slip_valid, slip_reason = await _validate_current_slip()
    if not slip_valid:
        logger.error("pinnacle wrong slip blocked before stake: %s", slip_reason)
        await _cleanup_slip_on_failure()
        return False, f"wrong_slip_blocked|{slip_reason}", Decimal("0"), ""

    from app.services.bookmakers.odds_change import (
        ODDS_CHANGE_ACCEPT_FLOOR,
        decide_odds_change,
    )

    try:
        requested_odds = float(odds)
    except Exception:
        requested_odds = float(ODDS_CHANGE_ACCEPT_FLOOR)

    # 从投注单读取当前赔率（@1.85 / 赔率 1.85 等）
    read_slip_odds_js = """() => {
      const body = String((document.body && document.body.innerText) || '').replace(/\\s+/g, ' ');
      const cands = [];
      const push = (n) => {
        const x = Number(n);
        if (x > 1.01 && x < 50) cands.push(x);
      };
      for (const re of [
        /@\\s*(\\d+\\.\\d{2,3})/g,
        /赔率\\s*[:：]?\\s*(\\d+\\.\\d{2,3})/g,
        /odds\\s*[:：]?\\s*(\\d+\\.\\d{2,3})/ig,
      ]) {
        let m;
        while ((m = re.exec(body)) !== null) push(m[1]);
      }
      // 投注单区域常见孤立亚赔
      const slipHint = /投下|投注单|总注金|风险|Place Bet|Stake/i.test(body);
      if (slipHint) {
        const loose = body.match(/(?:^|\\s)(\\d\\.\\d{2,3})(?:\\s|$)/g) || [];
        for (const s of loose.slice(0, 8)) {
          const n = String(s).trim();
          push(n);
        }
      }
      if (!cands.length) return null;
      // 取最接近常见亚赔、优先出现在 @ 后的第一个
      return cands[0];
    }"""

    async def _read_slip_odds() -> float | None:
        for fr in ([page] + list(getattr(page, "frames", []) or [])):
            try:
                v = await asyncio.wait_for(fr.evaluate(read_slip_odds_js), timeout=3.0)
                if v is not None:
                    return float(v)
            except Exception:
                continue
        return None

    slip_odds = await _read_slip_odds()
    ok_chg, why_chg, use_odds = decide_odds_change(requested_odds, slip_odds)
    if not ok_chg:
        logger.info("pinnacle bet abort odds-change: %s", why_chg)
        await _cleanup_slip_on_failure()
        return False, f"odds_change_reject|{why_chg}", Decimal("0"), ""
    if use_odds is not None:
        odds = float(use_odds)
    logger.info(
        "pinnacle odds-change policy: %s (requested=%s slip=%s use=%s)",
        why_chg, requested_odds, slip_odds, odds,
    )

    # 勾选「接受更佳的赔率」；下跌仍 ≥1.7 时靠确认后的「接受变化」处理
    accept_better_js = """() => {
      const clicks = [];
      const nodes = Array.from(document.querySelectorAll('button, a, div[role="button"], span, label, input'));
      const labelOf = (el) => String(el.innerText || el.textContent || el.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim();
      for (const el of nodes) {
        const t = labelOf(el);
        if (!t || !/接受更佳的赔率|接受更好的赔率|Accept.+odds/i.test(t)) continue;
        try {
          const inp = (el.matches && el.matches('input[type="checkbox"]'))
            ? el
            : (el.querySelector && el.querySelector('input[type="checkbox"]'));
          if (inp && !inp.checked) { inp.click(); clicks.push('check'); }
          else if (!inp) { el.click(); clicks.push('label'); }
        } catch (e) {}
      }
      return clicks;
    }"""
    try:
        for fr in ([page] + list(getattr(page, "frames", []) or [])):
            try:
                clicks = await asyncio.wait_for(fr.evaluate(accept_better_js), timeout=3.0)
                if clicks:
                    logger.info("pinnacle accept-better-odds %s", clicks)
            except Exception:
                continue
    except Exception:
        pass

    fill_js = """(stake) => {
      const want = String(stake);
      const visible = (el) => {
        try {
          const st = window.getComputedStyle(el);
          if (!st || st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') return false;
          const r = el.getBoundingClientRect();
          return r.width > 2 && r.height > 2;
        } catch (e) { return false; }
      };
      const ctxOf = (el) => {
        let p = el;
        let t = '';
        for (let i = 0; i < 5 && p; i++) {
          t += ' ' + (p.innerText || p.getAttribute('aria-label') || '');
          p = p.parentElement;
        }
        return t.toLowerCase();
      };
      const inputs = Array.from(document.querySelectorAll('input, textarea, [contenteditable=\"true\"]'));
      let hit = null;
      for (const inp of inputs) {
        if (!visible(inp)) continue;
        const meta = ((inp.getAttribute('placeholder') || '') + ' ' + (inp.name || '') + ' ' + (inp.id || '') + ' ' + (inp.type || '') + ' ' + (inp.getAttribute('aria-label') || '')).toLowerCase();
        const ctx = ctxOf(inp);
        // 平博：优先「风险/本金/Stake」，避免填到「可赢」
        let score = 0;
        if (/投注金额|风险|本金|stake|risk|金额|投注额|wager/.test(meta + ' ' + ctx)) score += 10;
        if (/可赢|^赢$|派彩|to win|win amount|返回/.test(meta + ' ' + ctx)) score -= 10;
        if (inp.type === 'number' || inp.inputMode === 'decimal' || inp.inputMode === 'numeric') score += 2;
        if (/stake|amount|金额|投注|赌注|wager|risk/.test(meta)) score += 3;
        if (score < 3) continue;
        if (!hit || score > hit.score) hit = { el: inp, score };
      }
      if (!hit) {
        const nums = inputs.filter((inp) => {
          if (!visible(inp)) return false;
          const meta = ((inp.getAttribute('placeholder') || '') + ' '
            + (inp.getAttribute('aria-label') || '')).toLowerCase();
          if (/搜索|search/.test(meta)) return false;
          const numeric = inp.type === 'number' || inp.inputMode === 'decimal' || inp.inputMode === 'numeric';
          const inSlip = /投下\\s*\\d+\\s*注|投注单|总注金|最低投注|stake|place\\s*bet/i.test(ctxOf(inp));
          return numeric || inSlip;
        });
        // 投注单通常风险在前、可赢在后；只在数值框/注单容器内兜底。
        if (nums.length) hit = { el: nums[0], score: 1 };
      }
      if (!hit) return { ok: false, why: 'no_input' };
      const el = hit.el;
      try { el.focus(); el.click(); el.select && el.select(); } catch (e) {}
      if (el.isContentEditable) {
        el.textContent = want;
      } else {
        const proto = window.HTMLInputElement && window.HTMLInputElement.prototype;
        const desc = proto && Object.getOwnPropertyDescriptor(proto, 'value');
        if (desc && desc.set) desc.set.call(el, want); else el.value = want;
      }
      for (const ev of ['input', 'change', 'keyup']) {
        try { el.dispatchEvent(new Event(ev, { bubbles: true })); } catch (e) {}
      }
      try { el.dispatchEvent(new InputEvent('input', { bubbles: true, data: want, inputType: 'insertText' })); } catch (e) {}
      try { el.blur(); el.dispatchEvent(new Event('blur', { bubbles: true })); } catch (e) {}
      return { ok: true, why: 'filled', value: el.value || el.textContent || '', tag: el.tagName, score: hit.score };
    }"""

    filled = False
    stake_verified = False
    fill_detail = ""
    targets = []
    try:
        raw_targets = [page] + [fr for fr in (page.frames or []) if fr != getattr(page, "main_frame", None)]
    except Exception:
        raw_targets = [page]
    # 优先含投注单的 frame，避免填到壳层无关 input
    scored = []
    for fr in raw_targets:
        score = 0
        try:
            meta = await asyncio.wait_for(
                fr.evaluate(
                    """() => {
                      const t = String((document.body && document.body.innerText) || '');
                      return {
                        slip: t.includes('投下') || t.includes('投注单') || t.includes('总注金'),
                        len: t.length,
                      };
                    }"""
                ),
                timeout=2.5,
            )
            if (meta or {}).get("slip"):
                score += 10
            score += min(3, int((meta or {}).get("len") or 0) // 500)
        except Exception:
            score = 0
        scored.append((score, fr))
    scored.sort(key=lambda x: -x[0])
    targets = [fr for _, fr in scored] or raw_targets
    # 读回校验：fill_js 写入后读输入框真实值（React 受控组件可能回弹/清空；
    # number 输入框不支持文本选择，盲用键盘会把 10 追加成 1010 —— 实测教训）
    read_stake_js = """() => {
      const inputs = Array.from(document.querySelectorAll('input, [contenteditable="true"]'));
      for (const inp of inputs) {
        try {
          const st = window.getComputedStyle(inp);
          if (!st || st.display === 'none' || st.visibility === 'hidden') continue;
          const r = inp.getBoundingClientRect();
          if (r.width < 20 || r.height < 10) continue;
          const meta = ((inp.getAttribute('placeholder') || '') + ' ' + (inp.name || '') + ' ' + (inp.type || '')).toLowerCase();
          if (inp.type === 'number' || /金额|风险|stake|注金|risk/.test(meta)) {
            const v = inp.value != null ? String(inp.value) : String(inp.textContent || '');
            if (v) return v;
          }
        } catch (e) {}
      }
      return '';
    }"""

    # evaluate 清空输入框（number 框唯一可靠的清空方式）
    clear_stake_js = """() => {
      const inputs = Array.from(document.querySelectorAll('input, [contenteditable="true"]'));
      let hit = null;
      for (const inp of inputs) {
        try {
          const st = window.getComputedStyle(inp);
          if (!st || st.display === 'none' || st.visibility === 'hidden') continue;
          const r = inp.getBoundingClientRect();
          if (r.width < 20 || r.height < 10) continue;
          const meta = ((inp.getAttribute('placeholder') || '') + ' ' + (inp.name || '') + ' ' + (inp.type || '')).toLowerCase();
          if (inp.type === 'number' || /金额|风险|stake|注金|risk/.test(meta)) { hit = inp; break; }
        } catch (e) {}
      }
      if (!hit) return false;
      try { hit.focus(); } catch (e) {}
      const proto = window.HTMLInputElement && window.HTMLInputElement.prototype;
      const desc = proto && Object.getOwnPropertyDescriptor(proto, 'value');
      if (hit.isContentEditable) hit.textContent = '';
      else if (desc && desc.set) desc.set.call(hit, '');
      else hit.value = '';
      for (const ev of ['input', 'change']) {
        try { hit.dispatchEvent(new Event(ev, { bubbles: true })); } catch (e) {}
      }
      return true;
    }"""

    for fr in targets:
        try:
            data = await asyncio.wait_for(fr.evaluate(fill_js, float(stake)), timeout=5.0)
        except Exception as e:
            fill_detail = f"fill_fail:{e}"
            continue
        if isinstance(data, dict) and data.get("ok"):
            filled = True
            fill_detail = f"filled:{data.get('value')}"
            # 读回真实值：一致即完成，绝不追加键盘输入（number 框全选无效）
            got_v = ""
            try:
                got_v = str(await asyncio.wait_for(fr.evaluate(read_stake_js), timeout=2.5) or "")
            except Exception:
                got_v = ""
            if got_v and abs(_to_f(got_v) - float(stake)) < 0.005:
                fill_detail = f"filled:{got_v}:verified"
                stake_verified = True
                break
            # 不一致（回弹/清空/残留）：evaluate 清空后再键盘输入（此时框已空，
            # 追加无风险）
            try:
                await asyncio.wait_for(fr.evaluate(clear_stake_js), timeout=2.5)
                await page.keyboard.type(str(stake), delay=50)
                got2 = ""
                try:
                    got2 = str(await asyncio.wait_for(fr.evaluate(read_stake_js), timeout=2.5) or "")
                except Exception:
                    got2 = ""
                if got2 and abs(_to_f(got2) - float(stake)) < 0.005:
                    fill_detail = f"filled:{got2}:typed_verified"
                    stake_verified = True
                else:
                    fill_detail = f"filled:{got2 or '?'}:typed_unverified"
            except Exception:
                try:
                    loc = fr.locator(
                        'input[type="number"], input[inputmode="decimal"], input[placeholder*="金额"], input[placeholder*="风险"], input[placeholder*="Stake"]'
                    ).first
                    if await loc.count() > 0 and await loc.is_visible():
                        await loc.click(timeout=1500)
                        await loc.fill(str(stake))
                        loc_value = str(await loc.input_value(timeout=1500) or "")
                        if abs(_to_f(loc_value) - float(stake)) < 0.005:
                            fill_detail = f"filled:{loc_value}:loc_verified"
                            stake_verified = True
                except Exception:
                    pass
            break
    if not filled:
        await _cleanup_slip_on_failure()
        return False, fill_detail or "stake_input_missing", Decimal("0"), ""
    if not stake_verified:
        await _cleanup_slip_on_failure()
        return False, f"stake_write_unverified|{fill_detail}", Decimal("0"), ""

    await page.wait_for_timeout(500)

    # 校验投注单总注金是否接近目标（防止填到可赢/残留大额）
    verify_js = """(want) => {
      const body = String((document.body && document.body.innerText) || '');
      let got = NaN;
      const m1 = body.match(/总注金\\s*([0-9]+(?:\\.[0-9]+)?)\\s*CNY/i);
      if (m1) got = Number(m1[1]);
      else {
        const m2 = body.match(/风险\\s*([0-9]+(?:\\.[0-9]+)?)\\s*CNY/i);
        if (m2) got = Number(m2[1]);
      }
      const minM = body.match(/最低投注额\\s*([0-9]+(?:\\.[0-9]+)?)/);
      return { got, want: Number(want), minStake: minM ? Number(minM[1]) : null, bodyHas: /投下\\s*\\d+\\s*注/.test(body) };
    }"""
    actual_stake = Decimal(str(stake))
    try:
        for fr in targets:
            try:
                v = await asyncio.wait_for(fr.evaluate(verify_js, float(stake)), timeout=3.0)
            except Exception:
                continue
            if not isinstance(v, dict):
                continue
            got = v.get("got")
            min_stake_site = v.get("minStake")
            if (
                min_stake_site
                and min_stake_site == min_stake_site
                and float(min_stake_site) > float(stake)
            ):
                from app.ai.strategy_gates import resolve_site_minimum_stake

                actual_stake, reason = resolve_site_minimum_stake(
                    requested_stake=stake,
                    dynamic_stake=dynamic_stake if dynamic_stake is not None else stake,
                    site_minimum=Decimal(str(min_stake_site)),
                    max_stake=stake_cap if stake_cap is not None else stake,
                    available_balance=available_balance,
                )
                if actual_stake is None:
                    logger.warning(
                        "pinnacle bet abort %s requested=%s dynamic=%s minimum=%s cap=%s balance=%s",
                        reason, stake, dynamic_stake, min_stake_site, stake_cap, available_balance,
                    )
                    await _cleanup_slip_on_failure()
                    return False, reason, Decimal("0"), ""
                logger.info(
                    "pinnacle stake adjusted requested=%s dynamic=%s minimum=%s actual=%s",
                    stake, dynamic_stake, min_stake_site, actual_stake,
                )
                data = await asyncio.wait_for(fr.evaluate(fill_js, float(actual_stake)), timeout=5.0)
                if not isinstance(data, dict) or not data.get("ok"):
                    await _cleanup_slip_on_failure()
                    return False, "stake_adjust_refill_failed", Decimal("0"), ""
                adjusted_value = _to_f(data.get("value"))
                if abs(adjusted_value - float(actual_stake)) >= 0.005:
                    await _cleanup_slip_on_failure()
                    return False, "stake_adjust_refill_unverified", Decimal("0"), ""
                fill_detail = f"site_minimum_adjusted:{actual_stake}"
                got = actual_stake
            if got == got and abs(float(got) - float(actual_stake)) > max(
                0.2, float(actual_stake) * 0.3
            ):
                logger.warning("pinnacle stake mismatch want=%s got=%s, refill", actual_stake, got)
                try:
                    data = await asyncio.wait_for(fr.evaluate(fill_js, float(actual_stake)), timeout=5.0)
                    if isinstance(data, dict) and data.get("ok"):
                        # 纯 evaluate 重填：不再键盘输入（number 框追加风险）
                        fill_detail = f"refilled:{actual_stake}:was:{got}"
                except Exception:
                    pass
            break
    except Exception:
        pass

    # 残留「清空注单」确认弹窗：点「取消」保留已填注单（点「好的」会清掉
    # 刚填的金额，导致重试时反复看到此弹窗）
    try:
        for fr in ([page] + list(getattr(page, "frames", []) or [])):
            try:
                dismissed = await asyncio.wait_for(
                    fr.evaluate(
                        """() => {
                          const body = String((document.body && document.body.innerText) || '');
                          if (!body.includes('清空注单')) return false;
                          const nodes = Array.from(document.querySelectorAll('button, a, div[role="button"], span'));
                          for (const el of nodes) {
                            const t = String(el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                            if (t === '取消') {
                              try { el.click(); return true; } catch (e) {}
                            }
                          }
                          return false;
                        }"""
                    ),
                    timeout=2.5,
                )
                if dismissed:
                    logger.info("pinnacle dismissed leftover clear-slip modal")
                    await page.wait_for_timeout(500)
                    break
            except Exception:
                continue
    except Exception:
        pass

    # Re-check immediately before the irreversible click.  A live market can
    # rerender the slip after odds/stake updates; confirmation is allowed only
    # while the same team + full-game total + side + exact line is still shown.
    slip_valid, slip_reason = await _validate_current_slip()
    if not slip_valid:
        logger.error("pinnacle wrong slip blocked before confirm: %s", slip_reason)
        await _cleanup_slip_on_failure()
        return False, f"wrong_slip_blocked_before_confirm|{slip_reason}", Decimal("0"), ""

    if preview_only:
        # Real-site selector rehearsal: amount is filled and read back, but the
        # irreversible "投下 N 注" / confirmation code below is never reached.
        logger.info(
            "pinnacle preview ready (no submit) home=%s away=%s sel=%s line=%s stake=%s detail=%s",
            home,
            away,
            sel,
            line,
            actual_stake,
            fill_detail,
        )
        return False, f"preview_ready|{fill_detail}", actual_stake, ""

    # 平博真实确认流程（截图）：
    # 1) 投注单底部橙色「投下N注」
    # 2) 弹窗「确认投注」→ 点橙色「OK」（勿点标题/取消）
    step1_js = """() => {
      const nodes = Array.from(document.querySelectorAll('button, a, div[role="button"], span, div, input[type="button"], input[type="submit"]'))
        .sort((a, b) => a.querySelectorAll('*').length - b.querySelectorAll('*').length);
      const labelOf = (el) => String(el.innerText || el.textContent || el.value || '').replace(/\\s+/g, ' ').trim();
      const visible = (el) => {
        try {
          const st = window.getComputedStyle(el);
          if (!st || st.display === 'none' || st.visibility === 'hidden') return false;
          const r = el.getBoundingClientRect();
          return r.width > 20 && r.height > 16;
        } catch (e) { return false; }
      };
      const slip = String((document.body && document.body.innerText) || '').replace(/\\s+/g, ' ').slice(0, 240);
      const samples = [];
      // 按钮白名单：必须是可点击控件且文案精确匹配投注动作。
      // 说明文字（如「最低投注额 5」）会被旧的 /投注.*\\d+/ 宽正则误点 ——
      // span/div 需限制为短标签或明确按钮角色，禁止夹带数字的说明句
      const placeControl = (node) => {
        let root = node;
        let slipRoot = null;
        for (let depth = 0; root && depth < 9; root = root.parentElement, depth++) {
          const text = String(root.innerText || '');
          const hasStake = Array.from(root.querySelectorAll('input, textarea, [contenteditable="true"]'))
            .some((inp) => visible(inp));
          if (hasStake && /投下\\s*1\\s*注|总注金|最低投注|投注单|place\\s*1\\s*bet|stake/i.test(text)) {
            slipRoot = root;
            break;
          }
        }
        if (!slipRoot) return null;
        const control = node.matches('button, a, [role="button"], input')
          ? node : (node.closest('button, a, [role="button"], input') || node);
        if (!slipRoot.contains(control) || control.disabled || control.getAttribute('aria-disabled') === 'true') return null;
        return control;
      };
      for (const el of nodes) {
        if (!visible(el)) continue;
        const t = labelOf(el);
        if (!t || t.length > 30) continue;
        if (samples.length < 24) samples.push(t);
        if (
          /^投下\\s*1\\s*注$/.test(t)
          || /^place\\s*1\\s*bet$/i.test(t)
        ) {
          const control = placeControl(el);
          if (!control) continue;
          try { control.scrollIntoView({ block: 'center' }); } catch (e) {}
          try { control.click(); } catch (e) {
            try { control.dispatchEvent(new MouseEvent('click', { bubbles: true })); } catch (e2) {}
          }
          return { ok: true, text: t };
        }
      }
      return { ok: false, samples, slip };
    }"""
    step2_js = """() => {
      const nodes = Array.from(document.querySelectorAll('button, a, div[role="button"], span, div, input[type="button"], input[type="submit"]'));
      const labelOf = (el) => String(el.innerText || el.textContent || el.value || '').replace(/\\s+/g, ' ').trim();
      const visible = (el) => {
        try {
          const st = window.getComputedStyle(el);
          if (!st || st.display === 'none' || st.visibility === 'hidden') return false;
          const r = el.getBoundingClientRect();
          return r.width > 24 && r.height > 18;
        } catch (e) { return false; }
      };
      let modalRoot = null;
      for (const el of Array.from(document.querySelectorAll('div, section, aside'))) {
        const t = String(el.innerText || '');
        const isConfirm = (t.includes('您是否想要投注') || t.includes('确认投注'))
          && t.includes('取消') && /\\bOK\\b/i.test(t);
        if (isConfirm) {
          if (!modalRoot || t.length < String(modalRoot.innerText || '').length) modalRoot = el;
        }
      }
      const scope = modalRoot
        ? Array.from(modalRoot.querySelectorAll('button, a, div[role="button"], input, span, div'))
        : [];
      scope.sort((a, b) => a.querySelectorAll('*').length - b.querySelectorAll('*').length);
      for (const node of scope) {
        const t = labelOf(node);
        if (t !== 'OK' && t !== 'Ok' && t !== 'ok') continue;
        const el = node.matches('button, a, [role="button"], input')
          ? node : (node.closest('button, a, [role="button"]') || node);
        if (!visible(el)) continue;
        if (t === 'OK' || t === 'Ok' || t === 'ok') {
          try { el.click(); } catch (e) {
            try { el.dispatchEvent(new MouseEvent('click', { bubbles: true })); } catch (e2) {}
          }
          return { ok: true, text: t };
        }
      }
      const samples = [];
      for (const el of scope) {
        if (!visible(el)) continue;
        const t = labelOf(el);
        if (t && t.length <= 16 && samples.length < 12) samples.push(t);
      }
      return { ok: false, samples, hasModal: !!modalRoot };
    }"""

    confirm_detail = ""
    step1_ok = False
    # React 会在金额 blur 后短暂重渲染/禁用按钮，等待精确的「投下 1 注」
    # 控件变为可用，而不是立即判 place_btn_missing。
    for _wait in range(8):
        for fr in targets:
            try:
                data = await asyncio.wait_for(fr.evaluate(step1_js), timeout=5.0)
            except Exception:
                continue
            if isinstance(data, dict) and data.get("ok"):
                step1_ok = True
                confirm_detail = f"step1:{data.get('text')}"
                break
        if step1_ok:
            break
        await page.wait_for_timeout(300)
    if not step1_ok:
        try:
            btn = page.get_by_role(
                "button", name=re.compile(r"^投下\s*1\s*注$")
            ).first
            if await btn.count() > 0 and await btn.is_visible():
                await btn.click(timeout=3000)
                step1_ok = True
                confirm_detail = "step1:投下1注"
        except Exception:
            pass
    if not step1_ok:
        slip_hint = ""
        btn_samples = ""
        try:
            for fr in targets:
                try:
                    data = await asyncio.wait_for(fr.evaluate(step1_js), timeout=4.0)
                except Exception:
                    continue
                if isinstance(data, dict):
                    slip_hint = str(data.get("slip") or "")[:120]
                    btn_samples = str(data.get("samples") or "")[:200]
                    break
        except Exception:
            pass
        logger.warning(
            "pinnacle place_btn_missing samples=%s slip=%s",
            btn_samples, slip_hint[:80],
        )
        await _cleanup_slip_on_failure()
        return False, f"place_btn_missing|{fill_detail}|btns=[{btn_samples}]|slip={slip_hint[:80]}", Decimal("0"), ""

    await page.wait_for_timeout(800)
    step2_ok = False
    for _ in range(10):
        for fr in targets:
            try:
                data = await asyncio.wait_for(fr.evaluate(step2_js), timeout=4.0)
            except Exception:
                continue
            if isinstance(data, dict) and data.get("ok"):
                step2_ok = True
                confirm_detail = f"{confirm_detail}|step2:{data.get('text')}"
                break
        if step2_ok:
            break
        await page.wait_for_timeout(400)

    if not step2_ok:
        await _cleanup_slip_on_failure()
        return False, f"ok_modal_missing|{fill_detail}|{confirm_detail}", Decimal("0"), ""

    # 确认后若弹出赔率变化：再读一次，≥1.7 才点「接受变化并投注」
    live2 = await _read_slip_odds()
    ok2, why2, use2 = decide_odds_change(requested_odds, live2 if live2 is not None else slip_odds)
    if not ok2:
        logger.info("pinnacle bet abort after confirm odds-change: %s", why2)
        await _cleanup_slip_on_failure()
        return False, f"odds_change_reject|{why2}|{confirm_detail}", Decimal("0"), ""
    if use2 is not None:
        odds = float(use2)

    accept_js = """() => {
      const wants = ['接受变化并投注','接受并投注','接受变化'];
      for (const el of document.querySelectorAll('button, a, div[role="button"]')) {
        const t = String(el.innerText || '').replace(/\\s+/g, ' ').trim();
        if (wants.some((w) => t === w || t.includes(w))) {
          try { el.click(); return t; } catch (e) {}
        }
      }
      return '';
    }"""
    # 仅当变动后仍 ≥ 地板价时接受变化
    if float(odds) + 1e-9 >= float(ODDS_CHANGE_ACCEPT_FLOOR):
        for fr in targets:
            try:
                acc = await fr.evaluate(accept_js)
                if acc:
                    confirm_detail = f"{confirm_detail}|accept:{acc}|{why2}"
            except Exception:
                pass
    else:
        await _cleanup_slip_on_failure()
        return False, f"odds_change_reject|{why2}|{confirm_detail}", Decimal("0"), ""
    await page.wait_for_timeout(2200)

    # ── 提取成功确认弹窗中的订单号 ──
    # 平博确认下单后会弹成功提示，其中可能含订单号/确认码。
    # 在关闭弹窗前先尝试提取，作为真实订单确认依据。
    bet_ref = ""
    ref_js = """() => {
      // 1) 扫描所有可见弹窗/提示文本，提取订单号
      const out = [];
      for (const el of document.querySelectorAll('[role="alert"], [class*="modal" i], [class*="dialog" i], [class*="toast" i], [class*="message" i], [class*="notice" i], [class*="success" i], [class*="confirm" i]')) {
        const t = String(el.innerText || '').replace(/\\s+/g, ' ').trim();
        const st = window.getComputedStyle(el);
        if (!t || t.length < 4 || t.length > 500) continue;
        if (st && (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0')) continue;
        out.push(t);
      }
      // 2) body 中含订单号关键词的短句
      const body = String((document.body && document.body.innerText) || '');
      // 匹配: Bet ID, Order, Ticket, Wager Reference, Ref, 注单号, 确认号, 订单号
      // 注意: Wager Reference 需作为整体匹配，避免 "Reference" 单独命中
      const re = /(?:Bet\\s*ID|Wager\\s*Reference|Order|Ticket|Ref|注单号|确认号|订单号|编号)[:#\\s]*([A-Za-z0-9\\-]{6,20})/gi;
      const m = body.match(re);
      if (m) {
        for (const match of m.slice(0, 3)) {
          const idMatch = match.match(/([A-Za-z0-9\\-]{6,20})$/);
          if (idMatch) out.push('ref:' + idMatch[1]);
        }
      }
      // 3) URL 中可能含订单 ID（部分站点下单后 URL 变化）
      const url = String(window.location.href || '');
      const urlMatch = url.match(/(?:bet|order|ticket|wager)[/=]([A-Za-z0-9\\-]{6,20})/i);
      if (urlMatch) out.push('url:' + urlMatch[1]);
      return out.slice(0, 5).join(' ;; ');
    }"""
    for fr in targets:
        try:
            ref_text = await asyncio.wait_for(fr.evaluate(ref_js), timeout=3.0)
        except Exception:
            continue
        if ref_text:
            # 从提取结果中解析最可能的订单号
            for part in str(ref_text).split(" ;; "):
                part = part.strip()
                if part.startswith("ref:"):
                    bet_ref = part[4:].strip()
                    break
                if part.startswith("url:"):
                    bet_ref = part[4:].strip()
                    break
            if not bet_ref:
                # 尝试从整段文本中提取纯数字/字母编号
                id_match = re.search(r'([A-Za-z0-9\-]{8,20})', str(ref_text))
                if id_match:
                    bet_ref = id_match.group(1)
            if bet_ref:
                logger.info("pinnacle bet_ref extracted: %s (from: %s)", bet_ref, str(ref_text)[:120])
                confirm_detail = f"{confirm_detail}|ref:{bet_ref}"
            break

    # 捕获站点拒绝弹窗：step2 确认后若站点拒绝（盘口失效/限额/风控），
    # 会弹错误提示框，读出来写入 detail 便于定位（此时余额校验注定失败）
    reject_msg = ""
    reject_js = """() => {
      const out = [];
      // 1) 常规弹窗（modal/alert/dialog 类）
      for (const el of document.querySelectorAll('[role="alert"], [class*="modal" i], [class*="dialog" i], [class*="toast" i], [class*="message" i], [class*="notice" i]')) {
        const t = String(el.innerText || '').replace(/\\s+/g, ' ').trim();
        const st = window.getComputedStyle(el);
        if (!t || t.length < 4 || t.length > 400) continue;
        if (st && (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0')) continue;
        out.push(t.slice(0, 200));
      }
      // 2) body 中含拒绝关键词的短句
      const body = String((document.body && document.body.innerText) || '');
      for (const re of [/[^\\n]{0,30}(?:无法|失败|拒绝|已取消|不足|超过|限制|失效|错误|请稍后|无法处理|不能接受)[^\\n]{0,50}/g]) {
        const m = body.match(re);
        if (m) out.push(m[0].replace(/\\s+/g, ' ').slice(0, 200));
        if (out.length > 5) break;
      }
      return out.slice(0, 5).join(' ;; ');
    }"""
    for fr in targets:
        try:
            msg = await asyncio.wait_for(fr.evaluate(reject_js), timeout=3.0)
        except Exception:
            continue
        if msg:
            reject_msg = str(msg)[:300]
            break
    if reject_msg:
        logger.warning("pinnacle place rejected by site: %s", reject_msg)
        confirm_detail = f"{confirm_detail}|reject:{reject_msg}"

    # 站点结果提示与公告统一走受保护的白名单关闭器。它会连续处理多个
    # 遮挡层，但永远跳过「确认投注」与「清空注单」业务弹窗。
    dismissed = await dismiss_pinnacle_blocking_modals(page)
    if dismissed:
        confirm_detail = f"{confirm_detail}|dismissed:{str(dismissed)[:120]}"

    # 站点拒绝兜底清理：拒绝弹窗关闭后，站点可能仍把失效单留在投注单里
    # （盘口失效/限额被拒的单子不会自行消失）。仅当检测到拒绝类消息时清，
    # 成功路径（"成功/已接受"）绝不清，避免误删已成交展示。
    if reject_msg and any(
        k in reject_msg for k in ("当前选项不适用", "余额不足", "不能低于", "已取消", "无法", "失败", "限额", "拒绝", "不能接受", "失效", "错误")
    ):
        await _cleanup_slip_on_failure()
        return (
            False,
            f"site_rejected|{reject_msg}|{confirm_detail}",
            Decimal("0"),
            "",
        )

    return True, f"{result.get('sample')}|{fill_detail}|{confirm_detail}|odds:{odds}", actual_stake, bet_ref
