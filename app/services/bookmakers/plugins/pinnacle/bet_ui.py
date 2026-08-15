"""平博 DOM 下单兜底。"""
from __future__ import annotations

import asyncio
import logging
import re
from decimal import Decimal

logger = logging.getLogger(__name__)

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
) -> tuple[bool, str]:
    """
    平博 DOM 兜底：定位赛事行/盘口线 → 点大小球赔率 → 填金额并确认。
    返回 (clicked_confirm, detail)。
    """
    sel = (selection or "").lower()
    # 大小球 / 独赢（主客和）共用同一套 DOM 点选
    if sel in ("under", "u"):
        side_words = ["小", "under", "u", "低于"]
    elif sel in ("over", "o"):
        side_words = ["大", "over", "o", "高于"]
    elif sel == "home":
        side_words = [home, "主", "home", "1"] if home else ["主", "home"]
    elif sel == "away":
        side_words = [away, "客", "away", "2"] if away else ["客", "away"]
    elif sel == "draw":
        side_words = ["和", "平局", "draw", "x", "X"]
    else:
        side_words = ["大", "over", "o", "高于"]
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

    # 队名 token：完整名 + 片段（先不导航，避免打掉当前已渲染的滚球列表）
    tokens = []
    for t in (home, away):
        t = (t or "").strip()
        if not t:
            continue
        tokens.append(t)
        for n in (2, 3, 4, 5, 6):
            if len(t) >= n:
                tokens.append(t[:n])
                tokens.append(t[-n:])
    seen: set[str] = set()
    tokens = [x for x in tokens if not (x in seen or seen.add(x))]

    click_js = """(args) => {
              const { tokens, odds, sideWords, line } = args;
              const norm = (s) => String(s || '').replace(/\\s+/g, '').toLowerCase();
              const oddsTextOk = (t) => {
                const n = Number(String(t || '').replace(/[^0-9.]/g, ''));
                if (!n || n < 1.01 || n > 50) return false;
                return Math.abs(n - Number(odds)) <= 0.06;
              };
              const lineTxt = (line == null || line === '') ? '' : String(line);
              const nodes = Array.from(document.querySelectorAll('div, tr, li, section, article, a'));
              let row = null;
              let how = '';
              let bestScore = -1e9;
              for (const tok of (tokens || [])) {
                const tn = norm(tok);
                if (!tn || tn.length < 2) continue;
                for (const el of nodes) {
                  try {
                    const raw = el.innerText || '';
                    const t = norm(raw);
                    if (!t || t.length < 8 || t.length > 900) continue;
                    if (!t.includes(tn)) continue;
                    // 必须像「赛事行」：含赔率数字；避免点到纯队名短节点
                    const oddsHits = (raw.match(/(?:^|\\s)(\\d\\.\\d{2,3})(?:\\s|$)/g) || []).length;
                    if (oddsHits < 1) continue;
                    // 偏好含赔率、长度适中的行
                    const score = oddsHits * 100 - Math.abs(raw.length - 160);
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
                    if (!raw.includes(lineTxt)) continue;
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
              const scope = row || document.body;
              // 行级队名校验：目标行必须真正包含目标队名（长 token），否则禁止任何点击
              // （防「陶朗加市」2字片段误中「普雷斯顿」等无关行的赔率）
              let rowHasTeam = false;
              if (row) {
                for (const tok of (tokens || [])) {
                  const tn2 = norm(tok);
                  if (tn2 && tn2.length >= 3 && norm(row.innerText || '').includes(tn2)) { rowHasTeam = true; break; }
                }
              }
              if (row && !rowHasTeam) {
                return { ok: false, why: 'row_team_mismatch', sample: norm(row.innerText || '').slice(0, 120), how };
              }
              if (row) {
                try { row.scrollIntoView({ block: 'center' }); } catch (e) {}
              }
              const clickables = Array.from((scope || document).querySelectorAll('button, a, span, div, td, label'));
              for (const el of clickables) {
                const txt = String(el.innerText || el.textContent || '').trim();
                // 宽松赔率判定：1.01-50 的纯数字节点（不校验 ±0.06，用于漂移兜底）
                const looseOddsOk = /^\\d{1,2}\\.\\d{2,3}$/.test(txt.replace(/\\s/g, ''));
                if (!oddsTextOk(txt) && !looseOddsOk) continue;
                // 纯赔率数字节点优先（避免点到「小\\n2.21」整块导致投注单不开）
                if (/^\\d{1,2}\\.\\d{2,3}$/.test(txt.replace(/\\s/g, ''))) {
                  const p = el.closest('div, tr, li, section') || el.parentElement;
                  const ctx = String((p && p.innerText) || txt);
                  const hitSide = sideWords.some((w) => ctx.toLowerCase().includes(String(w).toLowerCase()));
                  const hitLine = lineTxt ? ctx.includes(lineTxt) : true;
                  if (hitSide && hitLine) { pureOdds = el; }
                  // 漂移兜底：side 匹配即可（大小球行内该 side 只有一个赔率）
                  if (hitSide && !sideLoose) { sideLoose = el; }
                }
                if (!oddsTextOk(txt)) continue;
                let ctx = '';
                try {
                  const p = el.closest('div, tr, li, section') || el.parentElement;
                  ctx = String((p && p.innerText) || txt);
                } catch (e) { ctx = txt; }
                const hitSide = sideWords.some((w) => ctx.toLowerCase().includes(String(w).toLowerCase()));
                const hitLine = lineTxt ? ctx.includes(lineTxt) : false;
                const inRow = !!(row && row.contains(el));
                const ouCell = /(?:^|[\\s])(大|小|over|under)(?:$|[\\s])/i.test(ctx) && !hitSide;
                if (!lineTxt) {
                  if (ouCell) continue;
                  if (hitSide) { target = el; how = (how || 'odds') + '+side'; break; }
                  if (inRow && !rowFallback) { rowFallback = el; }
                  continue;
                }
                if (hitSide && hitLine) { target = el; how = (how || 'odds') + '+side+line'; break; }
                if (hitLine && !target) { target = el; how = (how || 'odds') + '+line'; }
                if (!target) { target = el; how = (how || 'odds') + '+loose'; }
              }
              if (pureOdds) { target = pureOdds; how = (how || 'odds') + '+pure'; }
              if (!target && sideLoose) { target = sideLoose; how = (how || 'odds') + '+sideloose'; }
              if (!target && rowFallback) { target = rowFallback; how = (how || 'odds') + '+row'; }
              if (!target) {
                const body = norm(document.body && document.body.innerText || '');
                const idx = row ? norm(row.innerText || '').slice(0, 180) : body.slice(0, 180);
                return { ok: false, why: row ? 'odds_not_found' : 'row_not_found', sample: idx, how, odds };
              }
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
    }
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
            if (meta or {}).get("hello") or blen < 40:
                score -= 20
            else:
                score += min(4, blen // 200)
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

        # 滚球列表渲染等待：主 frame 文本过短（<1500字）说明 SPA 列表未出
        # （goto/refresh 后常见），等最多 12s 让列表渲染，避免立即 odds_not_found
        async def _wait_board_ready() -> None:
            try:
                for _ in range(12):
                    try:
                        blen = int(
                            await page.evaluate(
                                "() => ((document.body && document.body.innerText) || '').length"
                            )
                        )
                    except Exception:
                        blen = 0
                    if blen >= 1500:
                        return
                    await page.wait_for_timeout(1000)
            except Exception:
                pass

        await _wait_board_ready()

        for score, fr in ordered:
            if score < -10:
                continue
            try:
                data = await asyncio.wait_for(fr.evaluate(click_js, args), timeout=6.0)
            except Exception as e:
                miss = f"evaluate_fail:{e}"
                continue
            if isinstance(data, dict) and data.get("ok"):
                return data, "ok"
            if isinstance(data, dict):
                miss = f"{data.get('why')}:{(data.get('sample') or '')[:100]}"
        return None, miss

    # 若上一次已停在「确认投注」弹窗：直接点 OK（截图流程第 2 步）
    resume_ok_js = """() => {
      const body = String((document.body && document.body.innerText) || '');
      if (!body.includes('您是否想要投注') && !(body.includes('确认投注') && body.includes('取消'))) {
        return { ok: false, why: 'no_modal' };
      }
      const nodes = Array.from(document.querySelectorAll('button, a, div[role="button"], span, div, input'));
      for (const el of nodes) {
        const t = String(el.innerText || el.textContent || el.value || '').replace(/\\s+/g, ' ').trim();
        if (t === 'OK' || t === 'Ok' || t === 'ok') {
          try { el.click(); } catch (e) {
            try { el.dispatchEvent(new MouseEvent('click', { bubbles: true })); } catch (e2) {}
          }
          return { ok: true, text: t };
        }
      }
      return { ok: false, why: 'ok_missing' };
    }"""
    try:
        for fr in ([page] + list(getattr(page, "frames", []) or [])):
            try:
                resumed = await asyncio.wait_for(fr.evaluate(resume_ok_js), timeout=3.0)
            except Exception:
                continue
            if isinstance(resumed, dict) and resumed.get("ok"):
                await page.wait_for_timeout(2200)
                return True, f"resume_ok:{resumed.get('text')}"
    except Exception:
        pass
    try:
        if await page.get_by_text("您是否想要投注", exact=False).count() > 0:
            ok_btn = page.get_by_text("OK", exact=True).first
            if await ok_btn.count() > 0:
                await ok_btn.click(timeout=2500)
                await page.wait_for_timeout(2200)
                return True, "resume_ok:OK_text"
    except Exception:
        pass

    # 确保球类页：足球滚球 / 篮球滚球，避免停在冰球等其它 live 列表
    try:
        want_fb = "basket" not in sport_l
        cur_u = (page.url or "").lower()
        need_sport = (
            (want_fb and "soccer" not in cur_u and "football" not in cur_u)
            or ((not want_fb) and "basket" not in cur_u)
        )
        if need_sport or "ice" in cur_u or "hockey" in cur_u or "冰球" in (await page.evaluate("() => (document.body && document.body.innerText || '').slice(0,400)") or ""):
            for text in (("足球", "Soccer", "足球滚球") if want_fb else ("篮球", "Basketball", "篮球滚球")):
                try:
                    loc = page.get_by_text(text, exact=False).first
                    if await loc.count() > 0 and await loc.is_visible():
                        await loc.click(timeout=1800)
                        await page.wait_for_timeout(900)
                        break
                except Exception:
                    continue
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

    try:
        from app.services.bookmakers.venue_entry import activate_sportsbook_tabs, page_already_on_live_board

        if not await page_already_on_live_board(page):
            await activate_sportsbook_tabs(page, live_only=True, gentle=True)
            await page.wait_for_timeout(800)
        for text in ("滚球盘", "滚球", "In-Play"):
            try:
                loc = page.get_by_text(text, exact=False).first
                if await loc.count() > 0 and await loc.is_visible():
                    await loc.click(timeout=1500)
                    await page.wait_for_timeout(600)
                    break
            except Exception:
                continue
    except Exception:
        pass

    # 确保在滚球列表（搜索页/白屏时先恢复）；并清掉残留投注单（避免总注金污染/最低额失败）
    try:
        cur = (page.url or "").lower()
        if "/search" in cur or "/live" not in cur:
            from app.services.bookmakers.venue_entry import recover_pinnacle_live_list

            await recover_pinnacle_live_list(page)
            await page.wait_for_timeout(1000)
    except Exception:
        pass
    try:
        for fr in ([page] + list(getattr(page, "frames", []) or [])):
            try:
                cleared = await asyncio.wait_for(
                    fr.evaluate(
                        """() => {
                          const nodes = Array.from(document.querySelectorAll('button, a, div[role="button"], span'));
                          for (const el of nodes) {
                            const t = String(el.innerText || '').replace(/\\s+/g, ' ').trim();
                            if (t === '移除全部' || t === 'Remove All' || t === '清除全部') {
                              try { el.click(); return t; } catch (e) {}
                            }
                          }
                          return '';
                        }"""
                    ),
                    timeout=3.0,
                )
                if cleared:
                    logger.info("pinnacle removed slip: %s", cleared)
                    await page.wait_for_timeout(600)
                    break
            except Exception:
                continue
    except Exception:
        pass

    # 过滤输入仅用于列表内定位，绝不 Enter 跳转
    for q in ((home or "")[:8], (away or "")[:8]):
        if q:
            await _search_team(q)
            break

    # 优先 DOM/frame 点赔率（留在 /live）；locator 点队名容易进详情/搜索
    result = None
    last_miss = "ui_miss"
    result, last_miss = await _try_click_all_frames()
    if not (isinstance(result, dict) and result.get("ok")):
        try:
            for tok in ((home or "")[:6], (away or "")[:6]):
                if not tok or len(tok) < 2:
                    continue
                loc = page.get_by_text(tok, exact=False).first
                cnt = await loc.count()
                logger.warning("pinnacle team locator tok=%s count=%s", tok, cnt)
                if cnt <= 0:
                    continue
                try:
                    await loc.scroll_into_view_if_needed(timeout=2000)
                except Exception:
                    pass
                odds_txt = f"{float(odds):.3f}".rstrip("0").rstrip(".")
                variants = {odds_txt, f"{float(odds):.2f}", f"{float(odds):.3f}".rstrip("0").rstrip(".")}
                clicked = False
                for vt in variants:
                    if not vt:
                        continue
                    oloc = page.get_by_text(re.compile(rf"(?<![0-9.]){re.escape(vt)}(?![0-9])")).first
                    if await oloc.count() == 0:
                        continue
                    await oloc.click(timeout=3000)
                    result = {"ok": True, "sample": vt, "how": f"locator_odds:{tok}"}
                    clicked = True
                    break
                if clicked:
                    break
        except Exception as e:
            logger.debug("locator odds click: %s", e)
    if not (isinstance(result, dict) and result.get("ok")):
        return False, last_miss or "stay_page_miss"


    await page.wait_for_timeout(900)

    # 点赔率后必须出现投注单；否则回滚球列表再试一次点选
    async def _slip_ready() -> bool:
        # 投注单打开：投下N注 / 总注金 / 最低投注额 / 可赢 + 金额输入
        check = """() => {
          const t = String((document.body && document.body.innerText) || '');
          if (/投下\\s*\\d+\\s*注/.test(t) || /总注金\\s*[0-9.]+\s*CNY/i.test(t)) return true;
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
        logger.warning("pinnacle slip not open after odds click, recover live + retry click")
        try:
            from app.services.bookmakers.venue_entry import recover_pinnacle_live_list

            await recover_pinnacle_live_list(page)
            await page.wait_for_timeout(1200)
        except Exception:
            pass
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
            return False, f"slip_not_open|{last_miss}"
        await page.wait_for_timeout(1200)
        for _ in range(5):
            if await _slip_ready():
                break
            await page.wait_for_timeout(400)
        if not await _slip_ready():
            return False, f"slip_not_open_after_retry|clicked={result.get('sample')}|{result.get('how')}"

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
        return False, f"odds_change_reject|{why_chg}"
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
        const nums = inputs.filter((inp) => visible(inp) && (inp.type === 'number' || inp.inputMode === 'decimal' || inp.inputMode === 'numeric' || inp.type === 'text'));
        // 投注单通常风险在前、可赢在后 → 取第一个
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
      return { ok: true, why: 'filled', value: el.value || el.textContent || '', tag: el.tagName, score: hit.score };
    }"""

    filled = False
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
    for fr in targets:
        try:
            data = await asyncio.wait_for(fr.evaluate(fill_js, float(stake)), timeout=5.0)
        except Exception as e:
            fill_detail = f"fill_fail:{e}"
            continue
        if isinstance(data, dict) and data.get("ok"):
            filled = True
            fill_detail = f"filled:{data.get('value')}"
            # 再用键盘兜底（React 受控）
            try:
                await page.keyboard.press("Meta+a")
                await page.keyboard.press("Control+a")
                await page.keyboard.type(str(stake), delay=50)
                fill_detail = f"filled:{stake}:typed"
            except Exception:
                try:
                    loc = fr.locator(
                        'input[type="number"], input[inputmode="decimal"], input[placeholder*="金额"], input[placeholder*="风险"], input[placeholder*="Stake"]'
                    ).first
                    if await loc.count() > 0 and await loc.is_visible():
                        await loc.click(timeout=1500)
                        await loc.fill("")
                        await loc.type(str(stake), delay=40)
                        fill_detail = f"filled:{stake}:loc"
                except Exception:
                    pass
            break
    if not filled:
        return False, fill_detail or "stake_input_missing"

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
    try:
        for fr in targets:
            try:
                v = await asyncio.wait_for(fr.evaluate(verify_js, float(stake)), timeout=3.0)
            except Exception:
                continue
            if not isinstance(v, dict):
                continue
            got = v.get("got")
            if got == got and abs(float(got) - float(stake)) > max(0.2, float(stake) * 0.3):
                logger.warning("pinnacle stake mismatch want=%s got=%s, refill", stake, got)
                try:
                    data = await asyncio.wait_for(fr.evaluate(fill_js, float(stake)), timeout=5.0)
                    if isinstance(data, dict) and data.get("ok"):
                        await page.keyboard.press("Meta+a")
                        await page.keyboard.press("Control+a")
                        await page.keyboard.type(str(stake), delay=50)
                        fill_detail = f"refilled:{stake}:was:{got}"
                except Exception:
                    pass
            break
    except Exception:
        pass

    # 平博真实确认流程（截图）：
    # 1) 投注单底部橙色「投下N注」
    # 2) 弹窗「确认投注」→ 点橙色「OK」（勿点标题/取消）
    step1_js = """() => {
      const nodes = Array.from(document.querySelectorAll('button, a, div[role="button"], span, div'));
      const labelOf = (el) => String(el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
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
      for (const el of nodes) {
        if (!visible(el)) continue;
        const t = labelOf(el);
        if (!t || t.length > 24) continue;
        if (samples.length < 16) samples.push(t);
        if (
          /^投下\\s*\\d+\\s*注$/.test(t)
          || /投下\\s*\\d+\\s*注/.test(t)
          || /^place\\s*\\d+\\s*bet$/i.test(t)
          || t === 'Place Bet'
          || t === '确认投注'
          || /^立即投注$/.test(t)
        ) {
          try { el.scrollIntoView({ block: 'center' }); } catch (e) {}
          try { el.click(); } catch (e) {
            try { el.dispatchEvent(new MouseEvent('click', { bubbles: true })); } catch (e2) {}
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
        if (t.includes('您是否想要投注') || (t.includes('确认投注') && t.includes('取消') && /\\bOK\\b/i.test(t))) {
          if (!modalRoot || t.length < String(modalRoot.innerText || '').length) modalRoot = el;
        }
      }
      const scope = modalRoot
        ? Array.from(modalRoot.querySelectorAll('button, a, div[role="button"], span, div, input'))
        : nodes;
      for (const el of scope) {
        if (!visible(el)) continue;
        const t = labelOf(el);
        if (t === 'OK' || t === 'Ok' || t === 'ok') {
          try { el.click(); } catch (e) {
            try { el.dispatchEvent(new MouseEvent('click', { bubbles: true })); } catch (e2) {}
          }
          return { ok: true, text: t };
        }
      }
      for (const el of nodes) {
        if (!visible(el)) continue;
        if (labelOf(el) === 'OK') {
          try { el.click(); } catch (e) {}
          return { ok: true, text: 'OK' };
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
    for fr in targets:
        try:
            data = await asyncio.wait_for(fr.evaluate(step1_js), timeout=5.0)
        except Exception:
            continue
        if isinstance(data, dict) and data.get("ok"):
            step1_ok = True
            confirm_detail = f"step1:{data.get('text')}"
            break
    if not step1_ok:
        try:
            btn = page.get_by_text(re.compile(r"投下\s*\d+\s*注")).first
            if await btn.count() > 0 and await btn.is_visible():
                await btn.click(timeout=3000)
                step1_ok = True
                confirm_detail = "step1:投下N注"
        except Exception:
            pass
    if not step1_ok:
        slip_hint = ""
        try:
            for fr in targets:
                try:
                    data = await asyncio.wait_for(fr.evaluate(step1_js), timeout=4.0)
                except Exception:
                    continue
                if isinstance(data, dict):
                    slip_hint = str(data.get("slip") or data.get("samples") or "")[:180]
                    break
        except Exception:
            pass
        return False, f"place_btn_missing|{fill_detail}|{slip_hint}"

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
        try:
            ok_btn = page.get_by_role("button", name="OK").first
            if await ok_btn.count() > 0 and await ok_btn.is_visible():
                await ok_btn.click(timeout=2500)
                step2_ok = True
                confirm_detail = f"{confirm_detail}|step2:OK"
                break
        except Exception:
            pass
        try:
            ok_btn = page.get_by_text("OK", exact=True).first
            if await ok_btn.count() > 0 and await ok_btn.is_visible():
                await ok_btn.click(timeout=2500)
                step2_ok = True
                confirm_detail = f"{confirm_detail}|step2:OK_text"
                break
        except Exception:
            pass
        await page.wait_for_timeout(400)

    if not step2_ok:
        return False, f"ok_modal_missing|{fill_detail}|{confirm_detail}"

    # 确认后若弹出赔率变化：再读一次，≥1.7 才点「接受变化并投注」
    live2 = await _read_slip_odds()
    ok2, why2, use2 = decide_odds_change(requested_odds, live2 if live2 is not None else slip_odds)
    if not ok2:
        logger.info("pinnacle bet abort after confirm odds-change: %s", why2)
        return False, f"odds_change_reject|{why2}|{confirm_detail}"
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
        return False, f"odds_change_reject|{why2}|{confirm_detail}"
    await page.wait_for_timeout(2200)

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

    return True, f"{result.get('sample')}|{fill_detail}|{confirm_detail}|odds:{odds}"


