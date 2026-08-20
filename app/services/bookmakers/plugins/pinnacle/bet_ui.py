"""平博 DOM 下单兜底。"""
from __future__ import annotations

import asyncio
import logging
import re
from decimal import Decimal

logger = logging.getLogger(__name__)


def _to_f(v) -> float:
    """安全转 float（金额读回校验用）。"""
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return -1.0


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
    dynamic_stake: Decimal | None = None,
    stake_cap: Decimal | None = None,
    available_balance: Decimal | None = None,
) -> tuple[bool, str, Decimal]:
    """
    平博 DOM 兜底：定位赛事行/盘口线 → 点小球赔率 → 填金额并确认。
    返回 (clicked_confirm, detail, actual_stake)。
    """
    sel = (selection or "").lower()
    # 大小球双向 DOM 点选。反方向词按方向取（under 防误点大、over 防误点小）。
    # 注意：不含单字母 o/u —— ctx.includes("o") 会命中任意英文文本，side 校验失效
    if sel in ("under", "u"):
        side_words = ["小", "under", "低于"]
    elif sel in ("over", "o"):
        side_words = ["大", "over", "高于"]
    else:
        return False, "仅支持大小球", Decimal("0")
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

    # 进场先清残留结果弹窗：上次下单的「当前选项不适用于投注/成功」等弹窗
    # 若未关闭，会拦截本单点赔率的点击 → slip_not_open / 点不响应
    try:
        _pre_dismiss_js = """() => {
          for (const el of document.querySelectorAll('[role="alert"], [class*="modal" i], [class*="dialog" i], [class*="toast" i], [class*="notice" i]')) {
            const t = String(el.innerText || '').replace(/\\s+/g, ' ').trim();
            const st = window.getComputedStyle(el);
            if (!t || t.length < 4 || t.length > 500) continue;
            if (st && (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0')) continue;
            if (!/(当前选项不适用|余额不足|不能低于|已取消|无法|失败|成功|投注已|已接受|限额|拒绝|暂不)/.test(t)) continue;
            let scope = el.closest('[class*="modal" i], [class*="dialog" i], [class*="popup" i], [role="dialog"]') || document;
            for (const btn of Array.from(scope.querySelectorAll('button, a, div[role="button"], span'))) {
              const bt = String(btn.innerText || btn.textContent || '').replace(/\\s+/g, ' ').trim();
              if (/^(OK|Ok|ok|确定|好|好的|关闭|close|Close|暂不)$/.test(bt)) {
                try { btn.click(); return 'cleared:' + bt; } catch (e) {}
              }
            }
          }
          return '';
        }"""
        for fr in ([page] + list(getattr(page, "frames", []) or [])):
            try:
                cleared = await asyncio.wait_for(
                    fr.evaluate(_pre_dismiss_js), timeout=2.5
                )
            except Exception:
                continue
            if cleared:
                logger.info("pinnacle pre-dismiss leftover modal: %s", cleared)
                await page.wait_for_timeout(600)
                break
    except Exception:
        pass

    # 球种定向只允许一次确定性的 URL 跳转；不再点侧栏/Tab 触发额外路由。
    try:
        cur_url = (page.url or "").lower()
        wanted_sport = "basketball" if sport_l == "basketball" else "soccer"
        if f"/{wanted_sport}/" not in cur_url or "/live" not in cur_url:
            from urllib.parse import urlparse

            pu = urlparse(page.url or "")
            org = f"{pu.scheme}://{pu.netloc}" if pu.netloc else ""
            if org:
                try:
                    await page.goto(
                        f"{org}/zh-cn/compact/sports/{wanted_sport}/live",
                        wait_until="domcontentloaded",
                        timeout=45000,
                    )
                    await page.wait_for_timeout(4000)
                except Exception as e:
                    logger.warning("pinnacle ui: sport goto err: %s", e)
                logger.info("pinnacle ui: sport page ready url=%s", (page.url or "")[:100])
    except Exception as e:
        logger.warning("pinnacle ui: basketball nav failed: %s", e)

    click_js = """(args) => {
              const { tokens, odds, sideWords, line, homeN, awayN, selDir } = args;
              const norm = (s) => String(s || '').replace(/\\s+/g, '').toLowerCase();
              const oddsTextOk = (t) => {
                const n = Number(String(t || '').replace(/[^0-9.]/g, ''));
                if (!n || n < 1.01 || n > 50) return false;
                return Math.abs(n - Number(odds)) <= 0.06;
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
                return [lineTxt];
              })();
              const hitLineTxt = (ctx) => !lineTxt || lineAliases.some((a) => String(ctx || '').includes(a));
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
              // side 精确判定：赔率格的「紧邻上下文」须含目标方向词。粗父容器同时含
              // 同一格内可能混有多个方向标签，需检查紧邻上下文。
              // 用相邻兄弟/文本左右字窗口判定，且要求不含反方向词。
              // 小球赔率格必须有紧邻的小球方向词，避免误点相邻盘口。
              const sideNear = (el) => {
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
                const nl = near.toLowerCase();
                const anti = selDir === 'over' ? ['小'] : ['大'];
                const hit = sideWords.some((w) => nl.includes(String(w).toLowerCase()));
                const hitAnti = anti.some((w) => nl.includes(String(w).toLowerCase()));
                if (hit && !hitAnti) return true;
                return false;
              };
              for (const el of clickables) {
                const txt = String(el.innerText || el.textContent || '').trim();
                const pureNum = txt.replace(/\\s/g, '');
                const isPureOdds = /^\\d{1,2}\\.\\d{2,3}$/.test(pureNum);
                if (!oddsTextOk(txt) && !isPureOdds) continue;
                // 纯赔率数字节点（叶子）优先：容器块（\\xa0/换行包裹）点了投注单不开。
                // 漂移护栏 ≤0.25：目标 2.00 点到 1.50 属错行/市场大动，禁止盲点
                if (isPureOdds) {
                  const n = Number(pureNum.replace(/[^0-9.]/g, ''));
                  if (n && Math.abs(n - Number(odds)) <= 0.25) {
                    const p = el.closest('div, tr, li, section') || el.parentElement;
                    const ctx = String((p && p.innerText) || txt);
                                        const hitSide = sideNear(el);
                    const hitLine = lineTxt ? hitLineTxt(ctx) : true;
                    const isLeaf = !(el.children && el.children.length);
                    const leafBetter = !pureOdds || (pureOdds.children && pureOdds.children.length && isLeaf);
                    if (hitSide && hitLine && leafBetter) { pureOdds = el; }
                    // 漂移兜底：side 命中且线属于本场（行内含线）才收首个
                    if (hitSide && rowHasLineTxt && !sideLoose) { sideLoose = el; }
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
                // 删除 line-only / 裸 loose 兜底 —— 不校验方向，会点到反向/错盘
                if (hitSide && hitLine) { target = el; how = (how || 'odds') + '+side+line'; break; }
              }
              if (pureOdds) { target = pureOdds; how = (how || 'odds') + '+pure'; }
              if (!target && sideLoose) { target = sideLoose; how = (how || 'odds') + '+sideloose'; }
              // rowFallback 不校验方向：仅无盘口线信息（独赢）时可用
              if (!target && !lineTxt && rowFallback) { target = rowFallback; how = (how || 'odds') + '+row'; }
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
        "homeN": (home or "").strip(),
        "awayN": (away or "").strip(),
        "selDir": sel,
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
                return False, "not_on_sports_page", Decimal("0")
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
                  const scores = (t.match(/\\b\\d{1,2}-\\d{1,2}\\b/g) || []).length;
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
                    miss = f"{why}:{(data.get('sample') or '')[:100]}"
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
                return False, "stale_confirm_modal", Decimal("0")
    except Exception:
        pass
    try:
        if await page.get_by_text("您是否想要投注", exact=False).count() > 0:
            ok_btn = page.get_by_text("OK", exact=True).first
            if await ok_btn.count() > 0:
                await ok_btn.click(timeout=2500)
                await page.wait_for_timeout(2200)
                return False, "stale_confirm_modal", Decimal("0")
    except Exception:
        pass

    # 上方已经按目标球类做过一次确定性跳转；这里禁止再点球类 Tab。
    try:
        want_fb = "basket" not in sport_l
        cur_u = (page.url or "").lower()
        need_sport = (
            (want_fb and "soccer" not in cur_u and "football" not in cur_u)
            or ((not want_fb) and "basket" not in cur_u)
        )
        if need_sport or "ice" in cur_u or "hockey" in cur_u or "冰球" in (await page.evaluate("() => (document.body && document.body.innerText || '').slice(0,400)") or ""):
            return False, "wrong_sport_page", Decimal("0")
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

    # 必须留在目标体育列表；后台/下单均不得把平博带到其它路由。
    try:
        cur = (page.url or "").lower()
        if "/search" in cur or "/compact/sports/" not in cur or "/live" not in cur:
            return False, "not_on_live_sports_page", Decimal("0")
    except Exception:
        return False, "sports_page_unavailable", Decimal("0")
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

    async def _cleanup_slip_on_failure() -> None:
        """下单失败后立即清空投注单残留：移除全部 + 确认弹窗点「好的」。

        失败出口若不清，残留注单会在投注单里漂移：赔率变化弹横幅、
        被下一次误点「投下N注」提交成废单。幂等，无残留时静默返回。
        """
        try:
            for fr in ([page] + list(getattr(page, "frames", []) or [])):
                try:
                    hit = await asyncio.wait_for(
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
                    if hit:
                        await page.wait_for_timeout(700)
                        # 「清空注单」确认弹窗：点「好的」确认清空
                        # （与注额保留场景点「取消」相反）
                        for fr2 in ([page] + list(getattr(page, "frames", []) or [])):
                            try:
                                confirmed = await asyncio.wait_for(
                                    fr2.evaluate(
                                        """() => {
                                          const body = String((document.body && document.body.innerText) || '');
                                          if (!body.includes('清空注单')) return false;
                                          const nodes = Array.from(document.querySelectorAll('button, a, div[role="button"], span'));
                                          for (const el of nodes) {
                                            const t = String(el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                                            if (t === '好的' || t === '确定' || t === 'OK' || t === '确认') {
                                              try { el.click(); return true; } catch (e) {}
                                            }
                                          }
                                          return false;
                                        }"""
                                    ),
                                    timeout=2.5,
                                )
                                if confirmed:
                                    break
                            except Exception:
                                continue
                        logger.info("pinnacle slip cleaned after failed bet (removed=%s)", hit)
                        return
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
                # 跳过纯短数字变体（odds=2.00 → "2"）：全页 get_by_text 会误中任意
                # 独立"2"（时间/比分/页码），点到错误赔率开错注单
                variants = {v for v in variants if len(v) >= 4}
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
        return False, last_miss or "stay_page_miss", Decimal("0")


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
            return False, f"slip_not_open|{last_miss}", Decimal("0")
        await page.wait_for_timeout(1200)
        for _ in range(5):
            if await _slip_ready():
                break
            await page.wait_for_timeout(400)
        if not await _slip_ready():
            return False, f"slip_not_open_after_retry|clicked={result.get('sample')}|{result.get('how')}", Decimal("0")

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
        return False, f"odds_change_reject|{why_chg}", Decimal("0")
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
                        fill_detail = f"filled:{stake}:loc"
                except Exception:
                    pass
            break
    if not filled:
        await _cleanup_slip_on_failure()
        return False, fill_detail or "stake_input_missing", Decimal("0")

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
                    return False, reason, Decimal("0")
                logger.info(
                    "pinnacle stake adjusted requested=%s dynamic=%s minimum=%s actual=%s",
                    stake, dynamic_stake, min_stake_site, actual_stake,
                )
                data = await asyncio.wait_for(fr.evaluate(fill_js, float(actual_stake)), timeout=5.0)
                if not isinstance(data, dict) or not data.get("ok"):
                    await _cleanup_slip_on_failure()
                    return False, "stake_adjust_refill_failed", Decimal("0")
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

    # 平博真实确认流程（截图）：
    # 1) 投注单底部橙色「投下N注」
    # 2) 弹窗「确认投注」→ 点橙色「OK」（勿点标题/取消）
    step1_js = """() => {
      const nodes = Array.from(document.querySelectorAll('button, a, div[role="button"], span, div, input[type="button"], input[type="submit"]'));
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
      const isClickable = (el) => {
        const tag = (el.tagName || '').toLowerCase();
        return tag === 'button' || tag === 'a' || el.getAttribute('role') === 'button'
          || (el.getAttribute('type') || '') === 'button' || (el.getAttribute('type') || '') === 'submit';
      };
      for (const el of nodes) {
        if (!visible(el)) continue;
        const t = labelOf(el);
        if (!t || t.length > 30) continue;
        if (samples.length < 24) samples.push(t);
        if (
          /^投下\\s*\\d+\\s*注$/.test(t)
          || /^place\\s*\\d+\\s*bet$/i.test(t)
          || t === 'Place Bet'
          || t === '确认投注'
          || /^立即投注$/.test(t)
          || /^投注$/.test(t)
          || /^下注$/.test(t)
          || /^bet$/i.test(t)
          || /^submit$/i.test(t)
        ) {
          if (!isClickable(el)) continue;
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
        # 最后兜底：在投注单区域找任意可点击按钮
        try:
            for sel in (
                'button:has-text("投注")',
                'button:has-text("下注")',
                'button:has-text("Bet")',
                '[role="button"]:has-text("投注")',
                '[role="button"]:has-text("下注")',
                'button[type="submit"]',
            ):
                try:
                    loc = page.locator(sel).last
                    if await loc.count() > 0 and await loc.is_visible():
                        await loc.click(timeout=3000)
                        step1_ok = True
                        confirm_detail = f"step1:fallback:{sel}"
                        break
                except Exception:
                    continue
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
        return False, f"place_btn_missing|{fill_detail}|btns=[{btn_samples}]|slip={slip_hint[:80]}", Decimal("0")

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
        await _cleanup_slip_on_failure()
        return False, f"ok_modal_missing|{fill_detail}|{confirm_detail}", Decimal("0")

    # 确认后若弹出赔率变化：再读一次，≥1.7 才点「接受变化并投注」
    live2 = await _read_slip_odds()
    ok2, why2, use2 = decide_odds_change(requested_odds, live2 if live2 is not None else slip_odds)
    if not ok2:
        logger.info("pinnacle bet abort after confirm odds-change: %s", why2)
        await _cleanup_slip_on_failure()
        return False, f"odds_change_reject|{why2}|{confirm_detail}", Decimal("0")
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
        return False, f"odds_change_reject|{why2}|{confirm_detail}", Decimal("0")
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

    # 站点结果弹窗自动关闭：拒绝（当前选项不适用于投注/余额不足等）或
    # 成功提示都会弹框，残留会挡住后续下单（实测「当前选项不适用于投注」
    # 弹窗不关，下一单点赔率无响应）。读到消息就点 OK/确定/关闭
    dismiss_js = """(keywords) => {
      const close = (el) => {
        try {
          // 弹窗内的按钮优先（避免点到页面其它 OK）
          let scope = el.closest('[class*="modal" i], [class*="dialog" i], [class*="popup" i], [role="dialog"]') || document;
          for (const btn of Array.from(scope.querySelectorAll('button, a, div[role="button"], span'))) {
            const t = String(btn.innerText || btn.textContent || '').replace(/\\s+/g, ' ').trim();
            if (/^(OK|Ok|ok|确定|好|好的|关闭|close|Close|暂不)$/.test(t)) {
              try { btn.click(); return 'clicked:' + t; } catch (e) {}
            }
          }
          el.click();
          return 'el-clicked';
        } catch (e) { return ''; }
      };
      for (const el of document.querySelectorAll('[role="alert"], [class*="modal" i], [class*="dialog" i], [class*="toast" i], [class*="message" i], [class*="notice" i]')) {
        const t = String(el.innerText || '').replace(/\\s+/g, ' ').trim();
        const st = window.getComputedStyle(el);
        if (!t || t.length < 4 || t.length > 500) continue;
        if (st && (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0')) continue;
        if (!keywords.some((k) => t.includes(k))) continue;
        const r = close(el);
        if (r) return r + '|' + t.slice(0, 60);
      }
      return '';
    }"""
    dismiss_kws = ["当前选项不适用", "余额不足", "不能低于", "已取消", "无法", "失败", "成功", "投注已", "已接受", "限额", "拒绝", "暂不"]
    for fr in targets:
        try:
            done = await asyncio.wait_for(
                fr.evaluate(dismiss_js, dismiss_kws), timeout=3.0
            )
        except Exception:
            continue
        if done:
            logger.info("pinnacle result modal dismissed: %s", str(done)[:120])
            confirm_detail = f"{confirm_detail}|dismissed:{str(done)[:60]}"
            break
    await page.wait_for_timeout(600)

    # 站点拒绝兜底清理：拒绝弹窗关闭后，站点可能仍把失效单留在投注单里
    # （盘口失效/限额被拒的单子不会自行消失）。仅当检测到拒绝类消息时清，
    # 成功路径（"成功/已接受"）绝不清，避免误删已成交展示。
    if reject_msg and any(
        k in reject_msg for k in ("当前选项不适用", "余额不足", "不能低于", "已取消", "无法", "失败", "限额", "拒绝", "不能接受", "失效", "错误")
    ):
        await _cleanup_slip_on_failure()

    return True, f"{result.get('sample')}|{fill_detail}|{confirm_detail}|odds:{odds}", actual_stake
