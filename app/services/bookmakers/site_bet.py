"""
平博 / OB 真实下单（经 Browser Gate 长连接页）。

策略：
1. 优先用 odds_data._site 引用，向页面内已暴露的下注 API 发请求（拦截/探测）
2. 否则在体育页尝试按队名定位并点击对应盘口（兜底）
"""
from __future__ import annotations

import asyncio
import logging
import re
from decimal import Decimal
from typing import Any, Optional

from app.services.bookmakers.base import PlaceBetResult
from app.services.bookmakers.plugins.ob.odds import sanitize_token
from app.services.bookmakers.plugins.pinnacle.bet_ui import (
    ui_place_pinnacle_total as _ui_place_pinnacle_total,
)
from app.services.bookmakers.session_blob import apply_session_blob, is_session_blob
from app.services.bookmakers.site_profiles import get_site_profile

logger = logging.getLogger(__name__)


def selection_ref_from_site_odds(odds_data: dict, selection: str) -> Optional[dict]:
    if not isinstance(odds_data, dict):
        return None
    meta = odds_data.get("_site") or odds_data.get("_pinnacle") or odds_data.get("_ob")
    if not isinstance(meta, dict):
        return None
    sels = meta.get("selections") or {}
    ref = sels.get(selection)
    if not isinstance(ref, dict):
        return None
    out = dict(ref)
    out["mid"] = str(meta.get("mid") or out.get("mid") or "")
    out["site_code"] = str(meta.get("site_code") or "")
    out["bet_type"] = str(meta.get("bet_type") or "")
    out["line"] = meta.get("line")
    return out


async def _read_balance_from_page(page) -> Decimal:
    """优先读平博顶栏「xx.xx CNY 存款」，忽略投注单总注金/潜在奖金。"""
    try:
        data = await page.evaluate(
            """() => {
              const body = (document.body && (document.body.innerText || document.body.textContent)) || '';
              // 顶栏：6.77 CNY 存款
              let m = body.match(/\\b([0-9]+(?:\\.[0-9]{1,2}))\\s*CNY\\s*存款/i);
              if (m) return Number(m[1]);
              // 逐个 CNY，跳过投注单上下文
              const re = /\\b([0-9]+(?:\\.[0-9]{1,2}))\\s*CNY\\b/gi;
              let hit;
              while ((hit = re.exec(body))) {
                const ctx = body.slice(Math.max(0, hit.index - 24), hit.index + hit[0].length + 16);
                if (/总注金|潜在奖金|最低投注|投注金额|风险|本金|可赢/.test(ctx)) continue;
                return Number(hit[1]);
              }
              const kw = body.match(/(?:余额|Balance|钱包)[^\\d]{0,12}([0-9]+(?:\\.[0-9]{1,2})?)/i);
              if (kw) return Number(kw[1]);
              return null;
            }"""
        )
        if data is not None:
            return Decimal(str(data))
    except Exception:
        pass
    try:
        text = await page.inner_text("body")
        m = re.search(r"\b([0-9]+(?:\.[0-9]{1,2}))\s*CNY\s*存款", text, re.I)
        if not m:
            for m2 in re.finditer(r"\b([0-9]+(?:\.[0-9]{1,2}))\s*CNY\b", text, re.I):
                ctx = text[max(0, m2.start() - 24) : m2.end() + 16]
                if re.search(r"总注金|潜在奖金|最低投注|投注金额", ctx):
                    continue
                m = m2
                break
        if m:
            return Decimal(str(m.group(1)))
    except Exception:
        pass
    return Decimal("0")


def _parse_teams_from_external_id(match_external_id: str) -> tuple[str, str]:
    """pinnacle:dom|sport|league|home|away → (home, away)"""
    raw = (match_external_id or "").split(":", 1)[-1]
    parts = [p.strip() for p in raw.split("|") if p.strip()]
    if len(parts) >= 5:
        return parts[-2], parts[-1]
    if len(parts) >= 2:
        return parts[-2], parts[-1]
    return "", ""


async def place_site_bet(
    *,
    site_code: str,
    base_url: str,
    session_token: str,
    match_external_id: str,
    selection: str,
    odds: float,
    stake: Decimal,
    bet_type: str = "moneyline",
    odds_data: Optional[dict] = None,
    page=None,
    headed: bool = False,
    allow_launch: bool = False,
) -> PlaceBetResult:
    code = (site_code or "").lower()
    profile = get_site_profile(code)
    name = profile.get("name") or code
    token = sanitize_token(session_token)
    if not token or not base_url:
        return PlaceBetResult(ok=False, message=f"缺少 {name} 会话，请先在站点配置验证登录")

    ref = selection_ref_from_site_odds(odds_data or {}, selection)
    own_browser = False
    context = None
    browser = None
    pw = None

    try:
        page_closed = True
        try:
            page_closed = page is None or page.is_closed()
        except Exception:
            page_closed = True
        if page_closed:
            if not allow_launch:
                return PlaceBetResult(
                    ok=False,
                    message=f"无有效 {name} 长连接浏览器，请先验证登录（禁止另开窗口下单）",
                )
            from playwright.async_api import async_playwright

            from app.services.bookmakers.browser_login import DESKTOP_UA, DESKTOP_VIEWPORT

            pw = await async_playwright().start()
            browser = await pw.chromium.launch(
                headless=False,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            )
            context = await browser.new_context(
                ignore_https_errors=True,
                locale="zh-CN",
                viewport=dict(DESKTOP_VIEWPORT),
                user_agent=DESKTOP_UA,
            )
            page = await context.new_page()
            own_browser = True
            await page.goto(base_url.rstrip("/") + "/", wait_until="domcontentloaded", timeout=60000)
            if is_session_blob(token):
                await apply_session_blob(context, page, token)
            else:
                await page.evaluate(
                    """(t) => {
                      try {
                        localStorage.setItem('token', t);
                        localStorage.setItem('access_token', t);
                        localStorage.setItem('X-API-TOKEN', t);
                      } catch (e) {}
                    }""",
                    token,
                )
            path = (profile.get("sports_paths") or ["/"])[0]
            dest = path if str(path).startswith("http") else f"{base_url.rstrip('/')}{path}"
            await page.goto(dest, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(1500)

        # 解析队名必须在盘口恢复之前（平博 board recovery 依赖 home/away）
        home, away = _parse_teams_from_external_id(match_external_id)

        # 1) 页面内通用下注 POST：仅作探测；成功必须以余额扣减或明确订单号为准（禁止假成功）
        api_result = await page.evaluate(
            """async (args) => {
              const { ref, selection, odds, stake, betType, matchId } = args;
              const tryUrls = [
                '/api/bet', '/api/bet/place', '/bet/place', '/bet/order',
                '/sports/bet', '/member/bet', '/wager/place',
              ];
              const body = {
                selection, odds, stake, betType,
                matchId: (ref && ref.mid) || matchId || '',
                selectionId: (ref && (ref.oid || ref.id)) || '',
                marketId: (ref && ref.hid) || '',
                line: ref && ref.line,
              };
              for (const u of tryUrls) {
                try {
                  const resp = await fetch(u, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
                    credentials: 'include',
                    body: JSON.stringify(body),
                  });
                  const ct = (resp.headers.get('content-type') || '');
                  if (!ct.includes('json')) continue;
                  const data = await resp.json();
                  const ok = !!(data && (data.ok === true || data.success === true || data.code === 0 || data.status === 0));
                  if (ok || data?.orderId || data?.betId || data?.ticketId) {
                    return { ok: true, data, url: u };
                  }
                } catch (e) {}
              }
              return { ok: false };
            }""",
            {
                "ref": ref or {},
                "selection": selection,
                "odds": float(odds),
                "stake": float(stake),
                "betType": bet_type,
                "matchId": (match_external_id or "").split(":", 1)[-1],
            },
        )
        if isinstance(api_result, dict) and api_result.get("ok"):
            data = api_result.get("data") or {}
            order_id = str(
                data.get("orderId")
                or data.get("betId")
                or data.get("ticketId")
                or data.get("id")
                or ""
            ).strip()
            bal_before_api = await _read_balance_from_page(page)
            # 给站点结算一点时间再读余额
            await page.wait_for_timeout(1200)
            bal_after_api = await _read_balance_from_page(page)
            for _ in range(4):
                if (
                    bal_before_api > 0
                    and bal_after_api > 0
                    and bal_after_api <= bal_before_api - (stake * Decimal("0.5"))
                ):
                    break
                await page.wait_for_timeout(700)
                bal_after_api = await _read_balance_from_page(page)
            debited_api = (
                bal_before_api > 0
                and bal_after_api > 0
                and bal_after_api <= bal_before_api - (stake * Decimal("0.5"))
            )
            if debited_api or (order_id and order_id not in (f"{code}:{selection}", selection)):
                return PlaceBetResult(
                    ok=True,
                    message=f"{name} 下单成功（API{'·已扣款' if debited_api else '·订单号'}）",
                    external_bet_id=order_id or f"{code}:api:{selection}",
                    balance_after=bal_after_api if bal_after_api > 0 else bal_before_api,
                )
            logger.warning(
                "%s generic API claimed ok but no debit/order url=%s before=%s after=%s",
                code,
                api_result.get("url"),
                bal_before_api,
                bal_after_api,
            )

        # 2) UI 兜底（平博 DOM 盘口无 oid 时）：定位赛事行 → 点赔率 → 填注 → 确认
        #    必须以余额实际扣减为成功标准，避免假成功
        #    若当前不在滚球盘：只 gentle 点一次「滚球」，禁止 goto
        if code == "pinnacle":
            try:
                try:
                    cur_u = page.url or ""
                except Exception:
                    cur_u = ""
                fr_dump = []
                for fr in list(getattr(page, "frames", []) or [])[:12]:
                    try:
                        fu = (fr.url or "")[:100]
                        sample = await fr.evaluate(
                            "() => ((document.body && document.body.innerText) || '').replace(/\\s+/g,' ').slice(0,160)"
                        )
                        fr_dump.append(f"{fu} :: {(sample or '')[:120]}")
                    except Exception as e:
                        fr_dump.append(f"err:{e}")
                logger.warning(
                    "pinnacle place DOM dump url=%s frames=%s",
                    cur_u[:160],
                    " || ".join(fr_dump)[:900],
                )
            except Exception:
                pass
            try:
                from app.services.bookmakers.venue_entry import (
                    activate_sportsbook_tabs,
                    page_already_on_live_board,
                )

                async def _body_has_board() -> bool:
                    try:
                        t = await page.evaluate(
                            "() => ((document.body && document.body.innerText) || '')"
                        )
                    except Exception:
                        t = ""
                    t = t or ""
                    # 侧栏空壳也会有「体育/滚球」字样；要求滚球盘+盘口词+赔率数字
                    if not any(x in t for x in ("滚球盘", "投下", "投注单", "独赢")):
                        return False
                    if not any(x in t for x in ("让球", "独赢", "让分", "大小球", "@")):
                        return False
                    return bool(re.search(r"(?<![0-9])1\.\d{2,3}(?![0-9])", t))

                on_live = await page_already_on_live_board(page)
                has_board = await _body_has_board()
                try:
                    full_t = await page.evaluate(
                        "() => ((document.body && document.body.innerText) || '')"
                    )
                except Exception:
                    full_t = ""
                team_visible = bool(
                    (home and home[:4] and home[:4] in (full_t or ""))
                    or (away and away[:4] and away[:4] in (full_t or ""))
                )
                # 仅有导航壳/无目标队名时，强制软刷新拉起盘口列表
                shell_only = (
                    ("电子竞技" in (full_t or "") or "真人娱乐场" in (full_t or ""))
                    and "滚球盘" not in (full_t or "")
                    and not re.search(r"(?<![0-9])1\.\d{2,3}(?![0-9])", full_t or "")
                )
                logger.warning(
                    "pinnacle place board check on_live=%s has_board=%s team_visible=%s shell_only=%s",
                    on_live,
                    has_board,
                    team_visible,
                    shell_only,
                )
                need_recover = (not on_live) or (not has_board) or shell_only or (not team_visible)
                if need_recover:
                    logger.warning("pinnacle place: recover sports UI")
                    for text in ("体育", "Sports", "足球", "滚球盘", "滚球", "In-Play"):
                        try:
                            loc = page.get_by_text(text, exact=False).first
                            if await loc.count() > 0 and await loc.is_visible():
                                await loc.click(timeout=1800)
                                await page.wait_for_timeout(700)
                        except Exception:
                            continue
                    try:
                        await activate_sportsbook_tabs(page, live_only=True, gentle=True)
                    except Exception:
                        pass
                    await page.wait_for_timeout(1000)
                    if shell_only or not await _body_has_board() or not team_visible:
                        try:
                            cur = page.url or ""
                            logger.warning("pinnacle place: soft reload url=%s", cur[:140])
                            await page.reload(wait_until="domcontentloaded", timeout=45000)
                            await page.wait_for_timeout(3500)
                            for text in ("滚球盘", "滚球", "In-Play", "足球"):
                                try:
                                    loc = page.get_by_text(text, exact=False).first
                                    if await loc.count() > 0:
                                        await loc.click(timeout=1500)
                                        await page.wait_for_timeout(1200)
                                        break
                                except Exception:
                                    continue
                            logger.warning(
                                "pinnacle place after reload has_board=%s",
                                await _body_has_board(),
                            )
                        except Exception as e:
                            logger.warning("pinnacle soft reload failed: %s", e)
            except Exception as e:
                logger.debug("pinnacle pre-place live tab: %s", e)

        bal_before = await _read_balance_from_page(page)
        line = None
        sport = ""
        try:
            meta = (odds_data or {}).get("_site") or {}
            if meta.get("line") is not None:
                line = float(meta.get("line"))
            elif (odds_data or {}).get("line") is not None:
                line = float((odds_data or {}).get("line"))
            sport = str(meta.get("sport") or "")
        except Exception:
            line = None
        if not sport:
            # external_id: pinnacle:dom|football|...
            parts = (match_external_id or "").split(":", 1)[-1].split("|")
            if len(parts) >= 2:
                sport = parts[1]

        ui_ok = False
        ui_detail = ""
        bt = (bet_type or "").lower()
        if code == "pinnacle" and bt in (
            "total",
            "totals",
            "ou",
            "moneyline",
            "ml",
            "1x2",
            "spread",
            "handicap",
        ):
            ui_ok, ui_detail = await _ui_place_pinnacle_total(
                page,
                home=home,
                away=away,
                selection=selection,
                odds=float(odds),
                stake=stake,
                line=line,
                sport=sport,
            )
            logger.info("pinnacle ui place ok=%s detail=%s bt=%s", ui_ok, ui_detail, bt)
            # 赔率变动策略拒绝：直接返回可读原因（≥1.7 接受 / <1.7 放弃）
            if (not ui_ok) and ui_detail and "odds_change_reject" in str(ui_detail):
                reason = str(ui_detail).split("|", 1)[-1] if "|" in str(ui_detail) else str(ui_detail)
                return PlaceBetResult(
                    ok=False,
                    message=f"平博放弃下单：{reason}",
                    balance_after=bal_before,
                )
        else:
            # 通用弱匹配：仅按赔率文本点一次再填单
            odds_txt = f"{float(odds):.2f}".rstrip("0").rstrip(".")
            try:
                loc = page.locator(f"text=/{re.escape(odds_txt)}/").first
                if await loc.count() > 0:
                    await loc.click(timeout=4000)
                    ui_ok, ui_detail = True, "odds_text_click"
                    await page.wait_for_timeout(600)
            except Exception:
                ui_ok = False
            if ui_ok:
                try:
                    for sel_css in (
                        'input[type="number"]',
                        'input[placeholder*="金额"]',
                        'input[placeholder*="投注"]',
                        'input[name*="stake"]',
                        'input[name*="amount"]',
                    ):
                        inp = page.locator(sel_css).first
                        if await inp.count() > 0:
                            await inp.fill(str(stake))
                            break
                    for text in ("确认投注", "立即投注", "确认", "Place Bet", "Confirm", "投注"):
                        btn = page.get_by_text(text, exact=False).first
                        if await btn.count() > 0:
                            await btn.click(timeout=3000)
                            await page.wait_for_timeout(1200)
                            break
                except Exception as e:
                    ui_ok = False
                    ui_detail = f"confirm_fail:{e}"

        if ui_ok:
            bal_after = await _read_balance_from_page(page)
            # 余额刷新可能滞后：多轮重读（禁止 reload）
            for _ in range(8):
                if (
                    bal_before > 0
                    and bal_after > 0
                    and bal_after <= bal_before - (stake * Decimal("0.5"))
                ):
                    break
                await page.wait_for_timeout(900)
                bal_after = await _read_balance_from_page(page)
            debited = (
                bal_before > 0
                and bal_after > 0
                and bal_after <= bal_before - (stake * Decimal("0.5"))
            )
            if debited:
                # 下单成功后离开搜索页，回到滚球列表，避免赛事同步长期采空
                if code == "pinnacle":
                    try:
                        from app.services.bookmakers.venue_entry import (
                            page_is_off_match_list,
                            recover_pinnacle_live_list,
                        )

                        if await page_is_off_match_list(page):
                            await recover_pinnacle_live_list(page)
                    except Exception as e:
                        logger.debug("pinnacle post-bet recover list: %s", e)
                return PlaceBetResult(
                    ok=True,
                    message=f"{name} 下单成功（余额 {bal_before}→{bal_after}）",
                    external_bet_id=f"{code}:ui:{selection}:{home or 'x'}",
                    balance_after=bal_after,
                )
            logger.warning(
                "%s UI bet no debit before=%s after=%s stake=%s detail=%s",
                code,
                bal_before,
                bal_after,
                stake,
                ui_detail,
            )
            return PlaceBetResult(
                ok=False,
                message=(
                    f"{name} 页面已点投注但余额未扣减（{bal_before}→{bal_after}），"
                    f"未记成功。detail={ui_detail}"
                ),
                balance_after=bal_after if bal_after > 0 else bal_before,
            )

        if not ref:
            return PlaceBetResult(
                ok=False,
                message=(
                    f"{name} 盘口缺少投注参数且页面点击未成功（{ui_detail or 'ui_miss'}），"
                    "请确认浏览器在滚球列表可见目标赛事后再下单"
                ),
                balance_after=bal_before,
            )
        return PlaceBetResult(
            ok=False,
            message=f"{name} 自动下单未命中可用接口，请稍后重试或在站点内手动确认（已同步会话）",
            balance_after=bal_before,
        )
    except Exception as e:
        logger.exception("place_site_bet failed %s", code)
        return PlaceBetResult(ok=False, message=f"{name} 下单异常: {e}")
    finally:
        if own_browser:
            try:
                if browser:
                    await browser.close()
            except Exception:
                pass
            try:
                if pw:
                    await pw.stop()
            except Exception:
                pass
