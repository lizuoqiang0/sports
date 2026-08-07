"""
登录后进场馆：默认自动点击场馆入口；BOOKMAKER_MANUAL_VENUE=1 时改为等人操作。

OB / 平博：
1. 登录后自动点场馆名 /「进入游戏」等
2. 若落到 /404 则回首页一次再重试
3. 进入盘口后激活体育 Tab，再保持长连接
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# 场馆内体育盘口页特征（开云/平博通用）
_IN_VENUE_MARKERS = (
    "体育投注",
    "滚球盘",
    "滚球",
    "全场独赢",
    "独赢",
    "让分",
    "让球",
    "全场大小",
    "今日 (",
    "今日(",
    "今日（",
    "串关",
    "注单历史",
    "赛果",
    "In-Play",
    "Money Line",
    "Handicap",
    "大小球",
    "足球",
    "篮球",
    "棒球",
    "网球",
    "排球",
    "羽毛球",
    "乒乓球",
    "冰球",
    "台球",
    "电子竞技",
    "电竞",
    "体育赛事",
    "体育直播",
    "实时赛果",
    "投注",
    "赔率",
    "盘口",
    "赛事",
    "比赛",
    "联赛",
    "冠军",
    "锦标赛",
    "淘汰赛",
    "决赛",
    "半决赛",
    " Quarter",
    " Half Time",
    "Live Betting",
    "Sports Betting",
    "Basketball",
    "Football",
    "Tennis",
    "eSports",
)

# 强特征：出现任一即可认定已在盘口（避免大厅误判）
_STRONG_VENUE_MARKERS = (
    "全场独赢",
    "滚球盘",
    "注单历史",
    "全场大小",
    "让球",
    "让分",
    "香港盘",
    "亚洲盘",
    "亚盘",
    "赛果&比分",
    "赛果比分",
    "滚球",
    "独赢",
    "大小球",
    "半全场",
    "进球数",
    "角球",
    "黄牌",
    "红牌",
    "角球数",
    "比分",
    "波胆",
    "双重机会",
    "Team",
    "Over/Under",
    "Asian Handicap",
    "1X2",
    "Double Chance",
    "Correct Score",
    "Goal Total",
    "Corner",
    "Card",
)

# 赔率类型切换：全站统一亚洲盘（小数）
_EUROPEAN_ODDS_LABELS = (
    "亚洲盘",
    "亚盘",
    "亚赔",
    "欧洲",
    "Decimal",
    "EU",
    "Euro",
    "European",
)
_ODDS_TYPE_MENU_LABELS = (
    "赔率类型",
    "盘口类型",
    "赔率格式",
    "Odds Type",
    "Odds Format",
)


def _is_venue_url(url: str) -> bool:
    """
    盘口 URL 检测（动态适配场馆 URL 变化）。
    - 避免把 /game/sport 大厅入口或 404 误判为已进场馆
    - 支持带 token 的动态场馆 URL
    - 支持各站点的 H5/SPA 路由
    """
    u = (url or "").lower().strip()
    if not u or u in ("about:blank",):
        return False
    if u.startswith("chrome-error://") or u.startswith("chrome://") or u.startswith("devtools://"):
        return False
    if "/404" in u or u.rstrip("/").endswith("/404"):
        return False

    path_only = u.split("?")[0]
    # === 综合站 /game/sport/{品牌}：按品牌区分 ===
    # OB：/game/sport/ob?enName=YBTY 只是入口壳（中心钱包），无 H5/token 不算盘口
    brand_m = re.search(r"/game/sport/([^/?#]+)/?$", path_only)
    if brand_m:
        brand = (brand_m.group(1) or "").lower()
        if brand in ("ob", "ybty", "ky", "pm"):
            if "token=" not in u and "app-h5" not in u and "yewu" not in u:
                return False
    elif re.search(r"/game/sport/?$", path_only):
        return False
    if re.search(r"[?&]enname=ybty\b", u) and "token=" not in u and "app-h5" not in u and "yewu" not in u:
        return False

    # === 强特征 URL（命中即判定为场馆页）===
    # 真实 H5 / 业务域；禁止用 query 里的 enName=YBTY 冒充盘口
    if "app-h5" in u or "yewu11" in u or "yewu13" in u or "zlshelves" in u:
        return True
    if re.search(r"(?:^|[./_-])ybty(?:[./?#_-]|$)", u) or "/ybty/" in u or "ybty." in u:
        return True
    # 带 token 的动态场馆 URL（综合站通过 token 参数区分场馆）
    if ("token=" in u or "token%3d" in u) and any(x in u for x in ("sport", "match", "h5", "yewu", "venue", "game", "live")):
        return True
    # 带特定 query 参数的场馆页（不含 enName=YBTY 入口）
    if re.search(r'[?&](?:plat|venue|site|type|gameType|sportType)=', u) and any(
        x in u for x in ("sport", "match", "game", "live", "inplay", "h5", "mobile")
    ):
        return True

    # === 场馆 URL 关键词（多站点通用）===
    venue_keywords = (
        # 开云/OB 场馆
        "app-h5",
        "yewu11",
        "zlshelves",
        "/#/match",
        "sportstype",
        "obsport",
        "ob-sport",
        # 平博相关
        "compact/sports",
        "pinnacle",
        "pinny",
        # 通用体育场馆
        "sportsbook",
        "sport/index",
        "imsport",
        "sports/home",
        "sportbet",
        "sport-bet",
        "livecenter",
        "live-center",
        "inplay",
        "in-play",
        "im-sports",
        "imsports",
        "sportbook",
        "esport",
        "sportscn",
        "m-vgames",
        "vgames",
        "/sport/",
        "/sportsbook",
        "/bet/",
        "/odds/",
        "wap/sport",
        "mobile/sport",
        "h5/sport",
        "client/sport",
        "sport-live",
        "sports-live",
        "live-sports",
        "sports-bet",
        "sportsbet",
        "bet-sport",
        "sports-page",
        "venue/launch",
        "launch/sport",
        "/live/",
        "/inplay/",
        "/results/",
        "/fixtures/",
        "/events/",
        "/matches/",
        "sports_service",
        "sports-service",
        "sports_game",
        "sports-game",
    )
    if any(x in u for x in venue_keywords):
        return True

    # === SPA 路由风格的场馆 URL ===
    # 检测 /#/sports、/#/inplay、/#/match/:id 等 hash 路由
    if re.search(r'/#/(sports|sport|inplay|match|live|odds|fixture|event)', u):
        return True
    # 检测 /sports/:id、/inplay/:id 等 REST 风格路由
    path = urlparse(u).path if "://" in u else u
    if re.search(r'/(sports?|inplay|live|fixture|event|match|odds|bet|game)[/-][^/?#]+', path):
        return True

    # 带有体育相关 query 参数的页面
    if re.search(r'[?&](?:sport|sports|game|inplay|live|venue|match|odds|fixture|event)=', u):
        return True

    return False


def _is_dead_page_url(url: str) -> bool:
    u = (url or "").lower()
    return (
        not u
        or u in ("about:blank",)
        or u.startswith("chrome-error://")
        or u.startswith("chrome://")
        or "/404" in u
    )


async def capture_live_venue_url(page) -> str:
    """
    动态捕获当前场馆 URL（不写死路径）。
    以页面内容是否像盘口为主；URL 关键词仅作辅助。
    场馆域名/路径随时会变，调用方应每次进馆后刷新保存。
    """
    if page is None:
        return ""
    try:
        if page.is_closed():
            return ""
    except Exception:
        return ""

    # 优先检查当前页 + 各 frame（场馆常在新标签 / iframe）
    candidates = [page]
    try:
        ctx = page.context
        if ctx is not None:
            candidates = list(ctx.pages) or [page]
    except Exception:
        candidates = [page]

    for p in reversed(candidates):
        try:
            if p.is_closed():
                continue
        except Exception:
            continue
        try:
            u = (p.url or "").strip()
        except Exception:
            u = ""
        if not u or _is_dead_page_url(u):
            continue
        try:
            looks = await page_looks_like_sportsbook(p)
        except Exception:
            looks = False
        if looks or _is_venue_url(u):
            # 大厅选馆页不算
            try:
                if await page_looks_like_venue_lobby(p):
                    continue
            except Exception:
                pass
            return u
    return ""


async def page_looks_like_404(page) -> bool:
    try:
        url = (page.url or "").lower()
    except Exception:
        url = ""
    if "/404" in url or url.endswith("404"):
        return True
    try:
        text = await page.inner_text("body", timeout=2000)
    except Exception:
        return False
    return "页面找不到" in text or ("404" in (text or "")[:120] and "独赢" not in text)


async def _page_text_sample(page, limit: int = 4000) -> str:
    try:
        text = await page.inner_text("body", timeout=2500)
        return (text or "")[:limit]
    except Exception:
        return ""


async def page_looks_like_sportsbook(page) -> bool:
    """是否已在体育盘口页（含开云 /game/sport/ob）。"""
    try:
        if await page_looks_like_404(page):
            return False
    except Exception:
        pass
    try:
        url = page.url or ""
    except Exception:
        url = ""
    if _is_venue_url(url):
        return True

    text = await _page_text_sample(page)
    if not text:
        # iframe 内盘口
        try:
            for frame in page.frames:
                if frame == page.main_frame:
                    continue
                fu = (frame.url or "").lower()
                if _is_venue_url(fu):
                    return True
                try:
                    ft = await frame.inner_text("body", timeout=1500)
                except Exception:
                    continue
                if sum(1 for m in _IN_VENUE_MARKERS if m in (ft or "")) >= 2:
                    return True
                if any(m in (ft or "") for m in _STRONG_VENUE_MARKERS):
                    return True
        except Exception:
            pass
        return False

    # 1) 强特征词命中
    if any(m in text for m in _STRONG_VENUE_MARKERS):
        return True

    # 2) 通用特征词命中数
    hit = sum(1 for m in _IN_VENUE_MARKERS if m in text)
    if hit >= 2:
        return True

    # 3) 组合特征判断（降低误判）
    # 开云体育投注页
    if "体育投注" in text and ("今日" in text or "滚球" in text) and ("足球" in text or "篮球" in text):
        return True
    if "足球" in text and "篮球" in text and ("独赢" in text or "让" in text or "滚球" in text):
        return True
    # 体育赛事页
    if (
        "体育赛事" in text or "香港盘" in text or "亚洲盘" in text or "亚盘" in text
    ) and "滚球" in text and (
        "足球" in text or "篮球" in text
    ):
        return True
    # 通用组合：球类 + 投注/赔率/盘口
    if ("足球" in text or "篮球" in text or "棒球" in text or "网球" in text) and (
        "投注" in text or "赔率" in text or "盘口" in text or "赛事" in text or "比赛" in text
    ):
        return True
    # 英文组合
    if ("Football" in text or "Basketball" in text or "Tennis" in text) and (
        "Bet" in text or "Odds" in text or "Match" in text or "Game" in text or "Live" in text
    ):
        return True
    # 实时/滚球相关
    if ("滚球" in text or "实时" in text or "直播" in text or "In-Play" in text or "Live" in text) and (
        "赛事" in text or "比赛" in text or "Match" in text or "Game" in text
    ):
        return True

    return False


async def page_looks_like_venue_lobby(page) -> bool:
    """综合站大厅：有进入游戏，但还不是盘口页。"""
    try:
        url = page.url or ""
    except Exception:
        url = ""
    # URL 已是场馆 → 绝不当大厅
    if _is_venue_url(url):
        return False
    try:
        if await page_looks_like_404(page):
            return False
    except Exception:
        pass
    text = await _page_text_sample(page)
    if not text:
        return False
    # 已有盘口强特征 → 不是大厅
    if any(m in text for m in _STRONG_VENUE_MARKERS):
        return False
    if "体育投注" in text and ("今日" in text or "滚球" in text):
        return False
    # 已在盘口内容区（有滚球列表）→ 不是大厅
    if "滚球" in text and ("足球" in text or "篮球" in text) and (
        "香港盘" in text or "亚洲盘" in text or "亚盘" in text or "注单" in text or "赛果" in text
    ):
        return False
    has_enter = any(x in text for x in ("进入游戏", "进入场馆", "立即游戏", "开始游戏"))
    # 注意：不要把「体育赛事」当大厅标签——盘口页导航里也有
    has_pick = any(
        x in text
        for x in ("开云体育", "熊猫体育", "IM体育", "ONE体育")
    )
    return has_enter and has_pick


async def _click_first_text(page, texts: tuple[str, ...] | list[str], *, exact: bool = False) -> bool:
    for label in texts:
        if not label:
            continue
        try:
            loc = page.get_by_text(label, exact=exact).first
            if await loc.count() == 0:
                continue
            await loc.click(timeout=3500)
            return True
        except Exception:
            continue
    return False


async def ensure_european_odds_display(page) -> bool:
    """把场馆赔率类型切到亚洲盘（小数），便于分析与下单口径一致。"""
    switched = False
    try:
        # 常见站点偏好键：尽量写入后由前端自行刷新
        await page.evaluate(
            """() => {
              const keys = [
                'oddsType', 'odds_type', 'oddType', 'OddsType', 'priceType',
                'oddsFormat', 'odds_format', 'betOddsType', 'handicapType',
                'ODDS_TYPE', 'userOddsType', 'preferOddsType'
              ];
              const vals = ['EU', 'EURO', 'EUROPEAN', 'DECIMAL', '1', '2', '欧', '亚洲盘'];
              try {
                for (const k of keys) {
                  localStorage.setItem(k, 'EU');
                  sessionStorage.setItem(k, 'EU');
                }
                localStorage.setItem('oddsType', 'EU');
                localStorage.setItem('odds_type', 'EU');
                localStorage.setItem('oddsFormat', 'DECIMAL');
              } catch (e) {}
              return 'EU';
            }"""
        )
    except Exception:
        pass

    # 先点开类型菜单（若当前显示香港盘等）
    for menu in _ODDS_TYPE_MENU_LABELS:
        try:
            if await _click_first_text(page, (menu,), exact=False):
                await page.wait_for_timeout(250)
                break
        except Exception:
            continue

    for label in _EUROPEAN_ODDS_LABELS:
        try:
            if await _click_first_text(page, (label,), exact=False):
                switched = True
                await page.wait_for_timeout(600)
                break
        except Exception:
            continue

    if switched:
        logger.info("odds format switched to European (EU decimal)")
    return switched


async def dismiss_blocking_modals(page) -> None:
    """
    关掉挡点击的弹层（活动/公告/modal），避免滚球 Tab 点不到。

    重要：
    - 含「交易密码 / 支付密码」的弹窗只点关闭/取消，绝不点「确定/提交」
    - 绝不全局隐藏 .van-overlay（Vant 导航层被藏可能导致「系统错误」并跳回首页）
    """
    dismiss_js = """() => {
      const isTradePwd = (root) => {
        const t = String((root && (root.innerText || root.textContent)) || '');
        return /交易密码|支付密码|资金密码|提款密码|fund\\s*password|pay\\s*password|fundPassword/i.test(t)
          && t.length < 600;
      };
      const safeClose = ['关闭','取消','稍后','暂不','知道了','我知道了','跳过','忽略','×','X','Close','Cancel'];
      const confirmTxt = ['确定','确认','提交','完成','下一步','OK','Confirm','Verify'];

      const clickSafeClose = (root) => {
        const nodes = root.querySelectorAll(
          'button, a, span, i, [class*="close" i], [aria-label*="close" i], [aria-label*="关闭"]'
        );
        for (const el of nodes) {
          const t = String(
            el.innerText || el.textContent || el.getAttribute('aria-label') || ''
          ).trim();
          const cls = String(el.className || '');
          if (/close|cancel|icon-close|btn-close/i.test(cls) && t.length <= 16) {
            try { el.click(); return true; } catch (e) {}
          }
          if (!t || t.length > 12) continue;
          if (confirmTxt.some((x) => t === x)) continue;
          if (safeClose.some((x) => t === x)) {
            try { el.click(); return true; } catch (e) {}
          }
        }
        return false;
      };

      const hide = (el) => {
        try {
          el.style.setProperty('pointer-events', 'none', 'important');
          el.style.setProperty('display', 'none', 'important');
          el.setAttribute('aria-hidden', 'true');
        } catch (e) {}
      };

      const sel = '.modal, .modal-open, [role="dialog"], .ant-modal, .el-dialog, .van-dialog, .ivu-modal';
      let touched = 0;
      document.querySelectorAll(sel).forEach((m) => {
        let visible = true;
        try {
          const style = window.getComputedStyle(m);
          const rect = m.getBoundingClientRect();
          visible = style.display !== 'none' && style.visibility !== 'hidden'
            && rect.width > 60 && rect.height > 60;
        } catch (e) {}
        if (!visible) return;
        const trade = isTradePwd(m);
        // 交易密码：只尝试关闭/取消，绝不 hide（hide 易把场馆层一起搞挂）
        if (trade) {
          clickSafeClose(m);
          touched += 1;
          return;
        }
        const body = String(m.innerText || '').slice(0, 160);
        // 只处理明显公告/活动弹层；普通业务弹层不动
        if (!/公告|活动|优惠|欢迎|提示|消息|更新|维护|领取/.test(body)) return;
        clickSafeClose(m);
        const nodes = m.querySelectorAll('button, a, span');
        for (const el of nodes) {
          const t = String(el.innerText || el.textContent || '').trim();
          if (safeClose.concat(['知道了','我知道了']).some((x) => t === x)) {
            try { el.click(); } catch (e) {}
            break;
          }
        }
        // 仅隐藏该公告弹层本身，不碰全局 overlay
        try {
          const style = window.getComputedStyle(m);
          if (style && style.display !== 'none') hide(m);
        } catch (e) {}
        touched += 1;
      });
      return touched;
    }"""
    try:
        await page.evaluate(dismiss_js)
    except Exception:
        pass
    for fr in getattr(page, "frames", []) or []:
        try:
            if fr == page.main_frame:
                continue
            await fr.evaluate(dismiss_js)
        except Exception:
            continue


async def page_has_trade_password_modal(page) -> bool:
    """当前页（含 iframe）是否弹出交易/支付密码框。不含 body 全文，避免误判。"""
    js = """() => {
      const re = /交易密码|支付密码|资金密码|提款密码|fund\\s*password|pay\\s*password/i;
      const roots = Array.from(document.querySelectorAll(
        '.modal, .modal-open, [role="dialog"], .ant-modal, .el-dialog, .van-popup, .van-dialog, .ivu-modal'
      ));
      for (const r of roots) {
        try {
          const style = window.getComputedStyle(r);
          const rect = r.getBoundingClientRect();
          if (style.display === 'none' || style.visibility === 'hidden') continue;
          if (rect.width < 60 || rect.height < 60) continue;
        } catch (e) {}
        const t = String(r.innerText || r.textContent || '');
        if (re.test(t) && t.length < 600) return true;
      }
      // 独立 password 输入 + 交易密码文案（部分弹层无 dialog class）
      const inputs = Array.from(document.querySelectorAll('input[type="password"]'));
      for (const inp of inputs) {
        try {
          if (inp.offsetParent === null) continue;
        } catch (e) { continue; }
        let p = inp.parentElement;
        for (let i = 0; i < 5 && p; i++, p = p.parentElement) {
          const t = String(p.innerText || '');
          if (re.test(t) && t.length < 600) return true;
        }
      }
      return false;
    }"""
    try:
        if await page.evaluate(js):
            return True
    except Exception:
        pass
    for fr in getattr(page, "frames", []) or []:
        try:
            if fr == page.main_frame:
                continue
            if await fr.evaluate(js):
                return True
        except Exception:
            continue
    return False


async def page_has_system_error(page) -> bool:
    """站点是否弹出「系统错误」类提示（常见于部分场馆，随后会踢回首页）。"""
    js = """() => {
      const re = /系统错误|系统繁忙|操作失败|网络异常|服务异常|请重新登录/;
      const roots = Array.from(document.querySelectorAll(
        '.modal, .modal-open, [role="dialog"], .ant-modal, .el-dialog, .van-dialog, .van-toast, .van-notify, .toast, .message'
      ));
      for (const r of roots) {
        const t = String(r.innerText || r.textContent || '').trim();
        if (t && t.length < 200 && re.test(t)) return true;
      }
      return false;
    }"""
    try:
        if await page.evaluate(js):
            return True
    except Exception:
        pass
    for fr in getattr(page, "frames", []) or []:
        try:
            if fr == page.main_frame:
                continue
            if await fr.evaluate(js):
                return True
        except Exception:
            continue
    return False


def page_url_off_match_list(url: str) -> bool:
    """搜索/账户/投注详情等非赛事列表页（采盘应先离开）。"""
    u = (url or "").lower()
    if not u:
        return False
    return any(
        x in u
        for x in (
            "/compact/search",
            "/search/",
            "/my-bets",
            "/mybets",
            "/bet-history",
            "/account/",
            "/member/",
            "betslip",
            "/ticket/",
        )
    )


async def page_is_off_match_list(page) -> bool:
    try:
        if page is None or page.is_closed():
            return True
        return page_url_off_match_list(page.url or "")
    except Exception:
        return False
async def page_already_on_live_board(page) -> bool:
    """
    是否已在滚球盘/In-Play（OB H5 或平博 compact）。
    为 True 时同步路径禁止 goto / 切球类 / 反复点「滚球」。
    """
    try:
        if page is None or page.is_closed():
            return False
    except Exception:
        return False
    try:
        url = (page.url or "").lower()
    except Exception:
        url = ""
    # 搜索/账户页绝不是滚球盘（下注后常停在 compact/search）
    if page_url_off_match_list(url):
        return False
    # 平博：侧栏也有「滚球盘」文案，早盘 /sports/soccer 会被误判；必须 URL 带 /live
    if "rowilong" in url or "pinnacle" in url or "/compact/sports/" in url:
        if not any(x in url for x in ("/live", "in-play", "inplay")):
            return False
    # URL 强信号：必须带 live/in-play（勿把 compact/sports 早盘当成滚球）
    url_live = any(
        x in url
        for x in (
            "/live",
            "in-play",
            "inplay",
            "livecenter",
            "滚球",
        )
    )
    # OB H5：进场馆后由内容判定是否滚球，URL 仅作辅助
    ob_h5 = any(x in url for x in ("yewu", "app-h5", "zlshelves", "token="))
    if url_live:
        try:
            if await is_in_sportsbook(page) and not await page_looks_like_404(page):
                return True
        except Exception:
            pass

    async def _text_is_live(sample: str) -> bool:
        t = sample or ""
        if not t or len(t) < 12:
            return False
        # 必须「滚球盘」或 In-Play；单字「滚球」易命中侧栏空壳
        live_mark = any(
            x in t for x in ("滚球盘", "In-Play", "In Play", "LIVE Betting", "Live Betting")
        )
        board_mark = any(
            x in t for x in ("独赢", "让球", "让分", "Money Line", "Handicap")
        )
        # 还需像盘口数字（避免仅有导航文案）
        has_odds = bool(re.search(r"(?<![0-9])1\.\d{2,3}(?![0-9])", t))
        return bool(live_mark and board_mark and has_odds)

    try:
        text = await _page_text_sample(page)
        if await _text_is_live(text):
            return True
    except Exception:
        pass
    try:
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            try:
                fu = (frame.url or "").lower()
            except Exception:
                fu = ""
            if any(x in fu for x in ("/live", "in-play", "inplay")):
                return True
            try:
                ft = await frame.inner_text("body", timeout=1200)
            except Exception:
                continue
            if await _text_is_live(ft[:4000]):
                return True
            # OB H5 iframe：有盘口词 + 滚球相关即可
            if ob_h5 and await _text_is_live(ft[:4000]):
                return True
    except Exception:
        pass
    return False


async def activate_sportsbook_tabs(
    page,
    *,
    live_only: bool = False,
    gentle: bool = False,
) -> None:
    """已在盘口页内才切 Tab；大厅/404 不点。强制亚洲盘。

    gentle=True：少点乱点。
    已在滚球盘时：直接返回（OB/平博 都不再 goto/切 Tab）。
    """
    try:
        if await page_looks_like_404(page) or await page_looks_like_venue_lobby(page):
            return
        if not await page_looks_like_sportsbook(page):
            return
    except Exception:
        return

    try:
        if await page_has_trade_password_modal(page) or await page_has_system_error(page):
            logger.info("activate tabs skipped: trade-password or system-error modal")
            return
    except Exception:
        pass

    # 已在滚球盘：绝对停手
    try:
        if await page_already_on_live_board(page):
            logger.info("activate tabs skipped: already on live board")
            return
    except Exception:
        pass

    await dismiss_blocking_modals(page)
    if not gentle:
        await _click_first_text(page, ("体育投注", "体育博彩", "Sports Betting"), exact=False)
        await page.wait_for_timeout(400)
    try:
        await ensure_european_odds_display(page)
    except Exception as e:
        logger.debug("ensure european odds skipped: %s", e)

    if not gentle:
        for sport in ("足球", "篮球", "Football", "Soccer", "Basketball"):
            try:
                if await page_has_trade_password_modal(page) or await page_has_system_error(page):
                    return
                await dismiss_blocking_modals(page)
                loc = page.get_by_text(sport, exact=True).first
                if await loc.count() == 0:
                    loc = page.get_by_text(sport, exact=False).first
                if await loc.count() == 0:
                    continue
                try:
                    await loc.click(timeout=2500)
                except Exception:
                    try:
                        await loc.evaluate("el => el.click()")
                    except Exception:
                        continue
                await page.wait_for_timeout(900)
            except Exception:
                continue

    # 未在滚球时才尝试点一次滚球；gentle 点不到即停，绝不 goto
    await dismiss_blocking_modals(page)
    for text in ("滚球", "滚球盘", "直播", "Live", "In-Play", "进行中"):
        try:
            if await page_has_trade_password_modal(page) or await page_has_system_error(page):
                return
            loc = page.get_by_text(text, exact=False).first
            if await loc.count() == 0:
                continue
            try:
                await loc.click(timeout=1200 if gentle else 2500)
            except Exception:
                if gentle:
                    break
                try:
                    await loc.evaluate("el => el.click()")
                except Exception:
                    if await _click_first_text(page, (text,), exact=False):
                        pass
                    else:
                        continue
            await page.wait_for_timeout(500 if gentle else 800)
            break
        except Exception:
            continue
    if not live_only and not gentle:
        for text in ("全部",):
            if await _click_first_text(page, (text,), exact=False):
                await page.wait_for_timeout(500)
                break

async def recover_from_404(page, base_url: str) -> None:
    """若落在 404，只回首页一次，不再试其它路径。"""
    from app.services.bookmakers.browser_login import apply_desktop_viewport

    try:
        if not await page_looks_like_404(page):
            return
    except Exception:
        return
    base = (base_url or "").rstrip("/")
    logger.warning("page is 404, recover to home once: %s", base)
    try:
        await apply_desktop_viewport(page)
        await page.goto(base + "/", wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(600)
    except Exception as e:
        logger.warning("recover from 404 failed: %s", e)


async def wait_for_sportsbook_entry(
    page,
    *,
    context=None,
    timeout_ms: int = 90000,
    base_url: str = "",
) -> tuple[Any, str, bool]:
    """等待用户手动进入体育盘口。返回 (page, url, ok)。"""
    from app.services.bookmakers.browser_login import dismiss_h5_orient_tip

    ctx = context or getattr(page, "context", None)
    deadline = time.time() + max(8.0, timeout_ms / 1000.0)
    active = page
    recovered = False

    while time.time() < deadline:
        if base_url and not recovered:
            try:
                if await page_looks_like_404(page):
                    await recover_from_404(page, base_url)
                    recovered = True
            except Exception:
                pass

        candidates = [page]
        if ctx is not None:
            try:
                candidates = list(ctx.pages)
            except Exception:
                candidates = [page]

        for p in reversed(candidates):
            try:
                if p.is_closed():
                    continue
            except Exception:
                continue
            try:
                u = p.url or ""
            except Exception:
                u = ""
            if "/404" in (u or "").lower():
                continue
            # URL 优先：/game/sport、YBTY 等
            if _is_venue_url(u):
                active = p
                try:
                    await dismiss_h5_orient_tip(active)
                except Exception:
                    pass
                logger.info("sportsbook detected by url: %s", u[:160])
                return active, u, True
            try:
                in_book = await page_looks_like_sportsbook(p)
            except Exception:
                in_book = False
            if not in_book:
                continue
            try:
                on_lobby = await page_looks_like_venue_lobby(p)
            except Exception:
                on_lobby = False
            if on_lobby:
                continue
            active = p
            try:
                await dismiss_h5_orient_tip(active)
            except Exception:
                pass
            try:
                return active, (active.url or u), True
            except Exception:
                return active, u, True
        await page.wait_for_timeout(400)

    try:
        return active, (active.url or ""), False
    except Exception:
        return active, "", False


async def _auto_click_into_venue(page, *, site_code: str, context=None) -> Any:
    """自动点菜单 → 场馆名 → 进入游戏；可能打开新标签。"""
    from app.services.bookmakers.site_profiles import get_site_profile

    profile = get_site_profile(site_code)
    menus = tuple(profile.get("sports_menu_texts") or ())
    labels = tuple(profile.get("venue_labels") or ())
    entries = tuple(profile.get("sports_entry_texts") or ())

    active = page
    for round_i in range(3):
        try:
            if await page_looks_like_sportsbook(active) and not await page_looks_like_venue_lobby(active):
                return active
        except Exception:
            pass

        if menus:
            await _click_first_text(active, menus, exact=False)
            await active.wait_for_timeout(700)
        if labels:
            await _click_first_text(active, labels, exact=False)
            await active.wait_for_timeout(900)
        if entries:
            await _click_first_text(active, entries, exact=False)
            await active.wait_for_timeout(1200)

        # 新开页
        ctx = context or getattr(active, "context", None)
        if ctx is not None:
            try:
                pages = list(ctx.pages)
                if pages:
                    active = pages[-1]
                    # 禁止 bring_to_front：平博/OB 可见窗会被抢到系统前台
            except Exception:
                pass

        try:
            u = active.url or ""
        except Exception:
            u = ""
        if _is_venue_url(u):
            logger.info("auto venue url hit site=%s round=%s url=%s", site_code, round_i, u[:160])
            return active
    return active


async def enter_portal_venue(
    page,
    *,
    site_code: str,
    base_url: str,
    context=None,
    timeout_ms: int = 90000,
    force: bool = False,
    manual_venue: bool = False,
    wait_manual: Optional[bool] = None,
) -> tuple[Any, str]:
    """登录后进场馆：默认自动点击；manual_venue=True 时干等用户。"""
    from app.services.bookmakers.browser_login import dismiss_h5_orient_tip
    from app.services.bookmakers.site_profiles import get_site_profile

    code = (site_code or "ob").lower()
    profile = get_site_profile(code)
    base = (base_url or "").rstrip("/")
    ctx = context or getattr(page, "context", None)

    do_manual = bool(manual_venue) if wait_manual is None else bool(wait_manual)

    try:
        cur = page.url or ""
    except Exception:
        cur = ""

    try:
        already = await page_looks_like_sportsbook(page) and not await page_looks_like_venue_lobby(page)
        if not already and _is_venue_url(cur):
            already = True
    except Exception:
        already = False
    if already:
        await dismiss_h5_orient_tip(page)
        try:
            await activate_sportsbook_tabs(page, live_only=False)
        except Exception:
            pass
        logger.info("already in sportsbook site=%s url=%s", code, (cur or "")[:160])
        return page, cur

    await recover_from_404(page, base)

    if not do_manual:
        logger.info("auto entering venue site=%s", code)
        active = await _auto_click_into_venue(page, site_code=code, context=ctx)
        active, final_url, ok = await wait_for_sportsbook_entry(
            active,
            context=ctx,
            timeout_ms=max(20000, min(int(timeout_ms), 60000)),
            base_url=base,
        )
        if not ok:
            # 再点一轮
            active = await _auto_click_into_venue(active, site_code=code, context=ctx)
            active, final_url, ok = await wait_for_sportsbook_entry(
                active,
                context=ctx,
                timeout_ms=15000,
                base_url=base,
            )
        if ok:
            try:
                await activate_sportsbook_tabs(active, live_only=False)
            except Exception:
                pass
            logger.info("auto venue entered site=%s url=%s", code, (final_url or "")[:160])
        else:
            logger.warning("auto venue timeout site=%s url=%s", code, (final_url or "")[:160])
        return active, final_url

    labels = profile.get("venue_labels") or (profile.get("name"),)
    logger.info(
        "waiting manual venue site=%s labels=%s — 请手动进入指定场馆",
        code,
        labels,
    )
    active, final_url, ok = await wait_for_sportsbook_entry(
        page,
        context=ctx,
        timeout_ms=max(30000, timeout_ms),
        base_url=base,
    )
    if ok:
        try:
            await activate_sportsbook_tabs(active, live_only=False)
        except Exception:
            pass
        logger.info("manual venue entered site=%s url=%s", code, (final_url or "")[:160])
    else:
        logger.warning("manual venue timeout site=%s", code)
    return active, final_url


async def is_in_sportsbook(page) -> bool:
    """
    判断页面是否已在体育盘口。
    策略：URL 匹配 → 页面内容 → iframe 内容，逐层降级。
    重点：避免误判导致系统尝试 recover/goto 从而闪屏。
    """
    try:
        if page is None or page.is_closed():
            return False
    except Exception:
        return False
    try:
        url = page.url or ""
    except Exception:
        url = ""
    if _is_dead_page_url(url):
        return False

    # 1) URL 强匹配（已增强的 _is_venue_url）
    if _is_venue_url(url):
        try:
            if await page_looks_like_404(page):
                return False
        except Exception:
            pass
        return True

    # 2) 页面内容匹配
    try:
        if await page_looks_like_404(page):
            return False
        if await page_looks_like_sportsbook(page):
            return True
    except Exception:
        pass

    # 3) iframe 内容兜底（综合站场馆页常在 iframe 中加载）
    try:
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            try:
                fu = (frame.url or "").lower()
            except Exception:
                fu = ""
            if _is_venue_url(fu):
                return True
            try:
                ft = await frame.inner_text("body", timeout=2000)
            except Exception:
                continue
            if any(m in (ft or "") for m in _STRONG_VENUE_MARKERS):
                return True
            hits = sum(1 for m in _IN_VENUE_MARKERS if m in (ft or ""))
            if hits >= 2:
                return True
    except Exception:
        pass

    return False


def extract_token_from_venue_url(url: str) -> str:
    if not url:
        return ""
    m = re.search(r"[?&]token=([^&]+)", url, re.I)
    return m.group(1) if m else ""


# 兼容：平博专属逻辑已迁至 plugins.pinnacle.venue
from app.services.bookmakers.plugins.pinnacle.venue import (  # noqa: E402
    pinnacle_live_sport_urls,
    recover_pinnacle_live_list,
)

# 兼容：OB 场馆恢复已迁至 plugins.ob.venue
from app.services.bookmakers.plugins.ob.venue import (  # noqa: E402
    page_in_ob_venue,
    recover_ob_live_venue,
)
