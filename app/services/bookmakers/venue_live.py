"""
场馆页实时盘口采集（与综合站域名无关）。

流程：已在 OB/平博 场馆页 → 切足球/篮球/滚球 → 嗅探 XHR + DOM 兜底。
换门户不改这里，只依赖场馆 UI / JSON 结构。
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from app.services.bookmakers.base import RemoteMatch, RemoteOdds
from app.services.bookmakers.site_odds import parse_captured_payloads
from app.services.bookmakers.site_profiles import get_site_profile
from app.services.odds_domain import coerce_float_european, normalize_odds_data_to_european

logger = logging.getLogger(__name__)

_ODDS_RE = re.compile(r"^-?\d{1,3}(?:\.\d{1,3})?$")
_SCORE_RE = re.compile(r"^(\d{1,3})\s*[-:：]\s*(\d{1,3})$")
# 盘口页导航/公告/分类文案，禁止当队名入库
_UI_JUNK_RE = re.compile(
    r"投注单|待结算|最受欢迎|我的联赛|所有赛事|滚球盘|输赢盘|串关|登录|公告|偏好|"
    r"网址|连接到|耐心等待|禁用|电子竞技|亚洲界面|感谢您|全新|赛事列表|"
    r"可添加最爱|上限|今天|网球|排球|电竞|球队总得分|^过关$|^早盘$|"
    r"^体育$|^足球$|^篮球$|^比赛$|^今日|^感$|"
    r"^(?:[12]H|HT|FT|OT|ET|Q[1-4]|PEN|AH|H1|H2)$"
)


def _is_junk_team_name(name: str) -> bool:
    n = (name or "").strip().replace("\u200e", "").replace("\u200f", "")
    if len(n) < 2 or len(n) > 40:
        return True
    if n.isdigit():
        return True
    # 赔率串 / 纯数字小数不当队名
    if re.fullmatch(r"\d+(?:\.\d+)?", n):
        return True
    if re.fullmatch(r"\d{1,2}月\d{1,2}日?", n) or n.upper() in {"GMT", "UTC"}:
        return True
    if _UI_JUNK_RE.search(n):
        return True
    if n in {
        "足球",
        "篮球",
        "体育",
        "比赛",
        "滚球盘",
        "输赢盘",
        "投注单",
        "电子竞技",
        "感",
        "今天",
        "网球",
        "排球",
        "电竞",
        "过关",
        "早盘",
        # 「开始」可能是挪威球队 Start 的中译，不当 junk
        "未开始",
        "即将开始",
        "vs",
        "VS",
        "挪威",
        "瑞典",
        "丹麦",
        "芬兰",
        "1H",
        "2H",
        "HT",
        "FT",
    }:
        return True
    return False


_SCHEDULED_DATE_RE = re.compile(
    r"(?:^|[^\d])(?:\d{1,2}\s*[月/\-]\s*\d{4}|\d{4}[/\-]\d{1,2}[/\-]\d{1,2})"
    r"|(?:\d{1,2}\s*月\s*\d{4})"
    r"|(?:,\s*\d{2}:\d{2}:\d{2})",
    re.I,
)
_INPLAY_EVIDENCE_RE = re.compile(
    r"(?:上半场|下半场|中场|加时|点球|进行中|"
    r"第[一二三四1-4]\s*节|第\s*[1-4]\s*节|"
    r"(?:^|[^a-z0-9])(?:LIVE|Q[1-4]|1H|2H|HT|OT|ET|PEN)(?:$|[^a-z0-9]))",
    re.I,
)


def _raw_looks_scheduled_early(raw: str) -> bool:
    """行文本像早盘开赛时刻（含日期/未开始），且无进行中证据。"""
    t = (raw or "").strip()
    if not t:
        return False
    # 篮球高比分优先：有 20+ 比分则绝非早盘开球墙钟行
    if re.search(r"\b(?:[2-9]\d|[1-9]\d{2})\s*[-:：]\s*(?:[2-9]\d|[1-9]\d{2})\b", t):
        return False
    if _INPLAY_EVIDENCE_RE.search(t):
        return False
    if re.search(r"未开始|即将开始|Today|Tomorrow", t, re.I):
        return True
    return bool(_SCHEDULED_DATE_RE.search(t))


def _recover_teams_from_league(home: str, away: str, league: str) -> tuple[str, str]:
    """节次名当队名时，从 league「主 客」恢复。"""
    if not (_is_junk_team_name(home) or _is_junk_team_name(away)):
        return home, away
    raw = (league or "").replace("\u200e", " ").replace("\u200f", " ")
    parts = [p for p in re.split(r"\s+", raw.strip()) if p and not _is_junk_team_name(p)]
    if len(parts) < 2:
        return home, away
    ordered = sorted(parts[:4], key=lambda p: raw.find(p))
    return ordered[0][:100], ordered[1][:100]


def _parse_total_from_raw(raw: str, *, sport: str = "football") -> Optional[dict]:
    """从 DOM 行文本解析全场大小（大/小 + 盘口）。"""
    text = (raw or "").replace("\u200e", " ").replace("\u200f", " ")
    if not text or not re.search(r"大|小|Over|Under|\bO\b|\bU\b", text, re.I):
        return None
    # 平博 compact 的常见省略标签布局：
    #   <全场线> <大球赔率> 小 <小球赔率>
    # 页面不渲染「大」字，因此旧逻辑会从前面的让球 0.5-1 误取出 1.0。
    # 取最后一个匹配，避免同一赛事前面的让球/独赢数字抢先命中。
    # 显式含「大/Over」时必须走成对解析；否则 `大 2.5 1.85 小 2.5 1.75`
    # 会被 compact 规则截成 line=2.5/under=2.5。
    compact_matches = [] if re.search(r"(?:大|Over|\bO\b)", text, re.I) else list(re.finditer(
        r"(?:^|\s)(\d+(?:\.\d+)?(?:\s*-\s*\d+(?:\.\d+)?)?)\s+"
        r"(\d{1,2}\.\d{1,3})\s*(?:小|Under)\s*"
        r"(\d{1,2}\.\d{1,3})(?=$|\s)",
        text,
        re.I,
    ))
    if compact_matches:
        # 同一赛事可能同时出现半场线（如 1.0）和全场线（如 2-2.5）。
        # 全场大小球是本产品唯一允许的盘口，足球/篮球中通常对应更大的
        # 总线；选最大线，避免把半场/球队总分误写成全场。
        hit = max(
            compact_matches,
            key=lambda m: sum(
                float(x.strip()) for x in str(m.group(1)).split("-")
                if x.strip()
            ) / max(1, len(str(m.group(1)).split("-"))),
        )
        try:
            parts = [float(x.strip()) for x in str(hit.group(1)).split("-")]
            line = sum(parts) / len(parts)
            over = float(hit.group(2))
            under = float(hit.group(3))
        except (TypeError, ValueError):
            line = over = under = None
        if (
            line is not None and over is not None and under is not None
            and over > 1 and under > 1
            and ((sport == "football" and 0.5 <= line <= 12)
                 or (sport == "basketball" and 0.5 <= line <= 280))
        ):
            return {"line": line, "over": over, "under": under}
    m = re.search(
        r"(?:大|O(?:ver)?)\s*([0-9]+(?:\.[0-9]+)?)\s+(\d{1,2}\.\d{1,3}).{0,48}?"
        r"(?:小|U(?:nder)?)\s*(?:[0-9]+(?:\.[0-9]+)?\s+)?(\d{1,2}\.\d{1,3})",
        text,
        re.I,
    )
    over_first = True
    if not m:
        m = re.search(
            r"(?:小|U(?:nder)?)\s*([0-9]+(?:\.[0-9]+)?)\s+(\d{1,2}\.\d{1,3}).{0,48}?"
            r"(?:大|O(?:ver)?)\s*(?:[0-9]+(?:\.[0-9]+)?\s+)?(\d{1,2}\.\d{1,3})",
            text,
            re.I,
        )
        over_first = False
    if not m:
        # Fallback: 仅提取 under（OB H5 可能不显示 over）
        m_under = re.search(
            r"(?:小|U(?:nder)?)\s*([0-9]+(?:\.[0-9]+)?)\s+(\d{1,2}\.\d{1,3})",
            text, re.I,
        )
        if m_under:
            try:
                line_u = float(m_under.group(1))
                under_u = float(m_under.group(2))
            except (TypeError, ValueError):
                return None
            if under_u <= 1:
                return None
            if sport == "football" and not (0.5 <= line_u <= 12):
                return None
            if sport == "basketball" and not (100 <= line_u <= 280):
                if not (0.5 <= line_u <= 280):
                    return None
            return {"line": line_u, "under": under_u}
        # Fallback: 仅提取 over（平博 DOM 可能不显示 under）
        m_over = re.search(
            r"(?:大|O(?:ver)?)\s*([0-9]+(?:\.[0-9]+)?)\s+(\d{1,2}\.\d{1,3})",
            text, re.I,
        )
        if m_over:
            try:
                line_o = float(m_over.group(1))
                over_o = float(m_over.group(2))
            except (TypeError, ValueError):
                return None
            if over_o <= 1:
                return None
            if sport == "football" and not (0.5 <= line_o <= 12):
                return None
            if sport == "basketball" and not (100 <= line_o <= 280):
                if not (0.5 <= line_o <= 280):
                    return None
            return {"line": line_o, "over": over_o}
        return None
    try:
        line = float(m.group(1))
        a = float(m.group(2))
        b = float(m.group(3))
    except (TypeError, ValueError):
        return None
    if a <= 1 or b <= 1:
        return None
    # 球类盘口合理性
    if sport == "football" and not (0.5 <= line <= 12):
        return None
    if sport == "basketball" and not (100 <= line <= 280):
        # 允许未识别球类时的宽松线
        if not (0.5 <= line <= 280):
            return None
    if over_first:
        return {"line": line, "over": a, "under": b}
    return {"line": line, "under": a, "over": b}


async def capture_venue_payloads(
    page,
    captured: list[Any],
    *,
    wait_ms: int = 5000,
    soft: bool = False,
) -> None:
    """滚动/等待，让场馆页继续吐盘口 XHR（listener 需已挂上）。

    soft=True（平博）：少滚或不滚，避免 compact SPA 被滚动/重绘打成白屏。
    """
    try:
        if soft:
            # 加速：软等待上限约 2.2s（原 2–4.5s）
            await page.wait_for_timeout(max(900, min(wait_ms, 2200)))
            try:
                await page.mouse.wheel(0, 500)
                await page.wait_for_timeout(350)
            except Exception:
                pass
            return
        await page.wait_for_timeout(max(800, wait_ms // 3))
        await page.mouse.wheel(0, 1800)
        await page.wait_for_timeout(500)
        await page.mouse.wheel(0, 1800)
        await page.wait_for_timeout(max(700, wait_ms // 3))
    except Exception:
        try:
            await page.wait_for_timeout(min(wait_ms, 2500))
        except Exception:
            pass


async def scrape_dom_matches(page, *, site_code: str, live_only: bool = False, limit: int = 200) -> list[RemoteMatch]:
    """
    DOM 兜底：适配常见体育盘口页（体育投注 / 滚球盘 / 独赢·让分·大小）。
    不依赖某一综合站的 class 名。
    """
    url_sport = ""
    page_url = ""
    try:
        page_url = (page.url or "").lower()
        u = page_url
        if "basketball" in u or "/bk/" in u:
            url_sport = "basketball"
        elif "soccer" in u or "football" in u or "/fb/" in u:
            url_sport = "football"
    except Exception:
        url_sport = ""
        page_url = ""
    on_live_url = any(x in page_url for x in ("/live", "in-play", "inplay", "滚球"))

    try:
        # 主文档 + 同源 iframe（平博滚球列表偶发在 frame 内）
        targets = [page]
        try:
            for fr in page.frames:
                if fr == page.main_frame:
                    continue
                try:
                    fu = (fr.url or "").lower()
                except Exception:
                    fu = ""
                if any(x in fu for x in ("about:blank", "chrome-error")):
                    continue
                targets.append(fr)
        except Exception:
            targets = [page]

        raw = []
        for tgt in targets[:6]:
            try:
                part = await tgt.evaluate(
            """() => {
              const out = [];
              const textOf = (el) => (el && (el.innerText || el.textContent) || '')
                .replace(/[\\u200e\\u200f\\u00a0]/g, ' ')
                .replace(/\\s+/g, ' ')
                .trim();
              const isOdds = (t) => {
                if (!/^-?\\d{1,3}(?:\\.\\d{1,3})?$/.test(t)) return false;
                const n = Number(t);
                if (!Number.isFinite(n) || n === 0) return false;
                // 亚洲盘 ≥1.01；港盘/马来常见 0.xx 或负马来；拒绝明显比分/分钟
                if (Math.abs(n) > 100) return false;
                return true;
              };
              const scoreRe = /^(\\d{1,3})\\s*[-:：]\\s*(\\d{1,3})$/;
              const periodTok = /^(?:[12]H|HT|FT|OT|ET|Q[1-4]|PEN|AH|H1|H2|上半场|下半场|全场)$/i;

              // 联赛标题：必须像联赛/杯赛，禁止把「主 客」对阵行当联赛
              const leagueLike = (t) => {
                if (!t || t.length < 2 || t.length > 60) return false;
                if (isOdds(t) || scoreRe.test(t) || periodTok.test(t)) return false;
                const parts = t.split(' ').filter(Boolean);
                // 两个短中文词极像对阵，不当联赛
                if (parts.length === 2 && parts.every((p) => p.length >= 2 && p.length <= 16 && !isOdds(p))) {
                  if (!/联赛|杯|赛|锦标赛|League|Cup|Serie/.test(t)) return false;
                }
                return /联赛|杯|赛|NBA|CBA|NBL|超|甲|乙|锦标赛|League|Cup|Serie|Championship|Tournament/.test(t);
              };

              let currentLeague = '';
              let sportHint = '';
              const title = textOf(document.body).slice(0, 400);
              if (title.includes('篮球') || /今日\\s*[（(]?\\s*篮球/.test(title)) sportHint = 'basketball';
              if (title.includes('足球') || /今日\\s*[（(]?\\s*足球/.test(title)) sportHint = sportHint || 'football';

              const blocks = Array.from(document.querySelectorAll('div, section, li, tr, article'));
              for (const el of blocks) {
                const t = textOf(el);
                if (!t || t.length < 8 || t.length > 500) continue;
                // 过粗的容器跳过
                if (el.children && el.children.length > 40) continue;

                if (leagueLike(t) && el.children.length <= 6) {
                  currentLeague = t.slice(0, 80);
                  continue;
                }

                // 匹配行：至少两个队名片段 + 若干赔率
                const parts = t.split(' ').filter(Boolean);
                const odds = parts.filter(isOdds).map(Number);
                if (odds.length < 2) continue;

                // 纯节次盘口行（无对阵/无比分）跳过；勿误杀「Q3/上半场 + 比分」滚球行
                const hasVs = /\\svs\\s/i.test(t);
                const hasLiveScore = /\\b(?:[1-9]\\d{1,2})\\s*[-:：]\\s*(?:[1-9]\\d{1,2})\\b/.test(t)
                  || /\\b(?:[1-9]\\d)\\s*[-:：]\\s*(?:[1-9]\\d)\\b/.test(t);
                if (parts.some((p) => periodTok.test(p)) && !hasVs && !hasLiveScore) continue;

                const scoreHit = parts.find((p) => scoreRe.test(p));
                let hs = 0, as_ = 0;
                if (scoreHit) {
                  const m = scoreHit.match(scoreRe);
                  if (m) { hs = Number(m[1]); as_ = Number(m[2]); }
                }

                // 队名：平博紧凑盘优先「比分 节次 分钟' 主‎客‎」；其次「主 比分 客」/「A vs B」
                const junkName = /投注|结算|联赛|赛事|滚球|今日|串关|登录|公告|网址|界面|耐心|禁用|输赢|偏好|连接到|感谢|亚洲|电子竞技|待结算|最受欢迎|最爱|上限|网球|排球|电竞|球队总得分|^今天$|^未开始$|^即将开始$|^vs$|^(?:挪威|瑞典|丹麦|芬兰)$|^(?:[12]H|HT|FT|OT|ET|Q[1-4]|PEN|AH)$/i;
                let home = '';
                let away = '';
                // 平博：0-0  2H  13'  主队‎ 客队‎
                const pinOrder = t.match(/(\\d{1,3})\\s*[-:：]\\s*(\\d{1,3})\\s+(上半场|下半场|中场|加时|1H|2H|HT|Q[1-4]|第[一二三四1-4]\\s*节)\\s+(\\d{1,3})['′]\\s+([^\\u200e\\s][^\\u200e]{1,36}?)\\u200e\\s*([^\\u200e\\s][^\\u200e]{1,36}?)\\u200e?/);
                if (pinOrder && !junkName.test(pinOrder[5]) && !junkName.test(pinOrder[6])) {
                  hs = Number(pinOrder[1]); as_ = Number(pinOrder[2]);
                  home = (pinOrder[5] || '').replace(/[\\u200e\\u200f]/g, '').trim();
                  away = (pinOrder[6] || '').replace(/[\\u200e\\u200f]/g, '').trim();
                }
                const scorePair = !home ? t.match(/([\\u4e00-\\u9fffA-Za-z0-9.\\-·]{2,28})\\s+(\\d{1,3})\\s*[-:：]\\s*(\\d{1,3})\\s+([\\u4e00-\\u9fffA-Za-z0-9.\\-·]{2,28})/) : null;
                if (scorePair && !junkName.test(scorePair[1]) && !junkName.test(scorePair[4])) {
                  home = (scorePair[1] || '').trim();
                  away = (scorePair[4] || '').trim();
                  hs = Number(scorePair[2]); as_ = Number(scorePair[3]);
                }
                const vsHit = !home ? t.match(/([\\u4e00-\\u9fffA-Za-z0-9.\\-·]{2,28})\\s+vs\\s+([\\u4e00-\\u9fffA-Za-z0-9.\\-·]{2,28})/i) : null;
                if (vsHit) {
                  home = (vsHit[1] || '').trim();
                  away = (vsHit[2] || '').trim();
                }
                if (!home || !away || junkName.test(home) || junkName.test(away)) {
                  const names = parts.filter((p) =>
                    p.length >= 2 && p.length <= 30
                    && !isOdds(p) && !scoreRe.test(p) && !periodTok.test(p)
                    && !/^(LIVE|滚球|独赢|让分|大小|主|客|大|小|平|今日|串关|体育|足球|篮球|比赛|感|今天|网球|排球|早盘|过关|未开始|即将开始|vs)$/i.test(p)
                    && !junkName.test(p)
                    && !/^第.+节$/.test(p) && !/^\\d+['′分]?$/.test(p)
                    && !/^\\d{1,2}:\\d{2}$/.test(p)
                    && !/^周日|^周[一二三四五六]/.test(p)
                    && !/^\\d{1,2}月/.test(p)
                  );
                  if (names.length < 2) continue;
                  home = names[0];
                  away = names[1];
                }
                if (home === away) continue;
                if (junkName.test(home) || junkName.test(away)) continue;

                const period = (parts.find((p) => /第.+[节半场]|上半场|下半场|中场|加时|HT|FT|Q[1-4]|进行中/i.test(p)) || '');
                // 平博滚球常用 67' / 67′；也认 mm:ss
                const clock = (parts.find((p) => /^\\d{1,3}:\\d{2}$/.test(p) || /^\\d{1,3}['′]$/.test(p)) || '');
                const onLiveUrl = /\\/live(?:\\/|$|\\?)|in-play|inplay/i.test(String(location.href || ''));
                const clockLive = (() => {
                  const apostrophe = (clock || '').match(/^(\\d{1,3})['′]$/);
                  if (apostrophe) {
                    const mm = Number(apostrophe[1]);
                    return mm >= 1 && mm <= 130;
                  }
                  const m = (clock || '').match(/^(\\d{1,3}):(\\d{2})$/);
                  if (!m) return false;
                  const mm = Number(m[1]); const ss = Number(m[2]);
                  if (ss > 59 || mm < 1 || mm > 130) return false;
                  // :00/:15/:30/:45 且 ≥13 分：晚间开球墙钟（19:35/21:15），勿当比赛分钟
                  if (ss === 0 || ss === 30) return false;
                  if (mm >= 13 && (ss === 15 || ss === 45 || ss === 35 || ss === 5)) return false;
                  if (mm >= 13 && hs === 0 && as_ === 0) return false;
                  return true;
                })();
                const liveBadge = /(?:^|[\\s|·•])(?:LIVE|进行中)(?:$|[\\s|·•])/i.test(t);
                // 早盘开赛时刻行（含「02 8月 2026, 00:00:00」）绝不当滚球
                const scheduledEarly = /未开始|即将开始/.test(t)
                  || (/\\d{1,2}\\s*月\\s*\\d{4}|\\d{4}[\\/\\-]\\d{1,2}|,\\s*\\d{2}:\\d{2}:\\d{2}/.test(t)
                      && !/(?:LIVE|进行中|\\d{1,3}['′]|\\b\\d{1,3}:\\d{2}\\b)/i.test(t));
                const scoreLive = !!(scoreHit && (hs > 0 || as_ > 0));
                const strongPeriod = /上半场|下半场|中场|加时|第.+[节半场]|Q[1-4]|1H|2H/i.test(period || '');
                // 0-0 弱「进行中」+ mm:ss 易把开球墙钟当滚球；需强节次/角标/比分/撇号钟
                const apostropheClock = /^\\d{1,3}['′]$/.test(clock || '');
                const live = !scheduledEarly && (
                  strongPeriod || liveBadge || scoreLive || apostropheClock ||
                  (clockLive && (hs > 0 || as_ > 0))
                );

                // 全场大小：从行文本抽 大/小 + 盘口线 + 赔率
                let total_line = null, over = null, under = null;
                const ou = t.match(/(?:大|O(?:ver)?)\\s*([0-9]+(?:\\.[0-9]+)?)\\s+(\\d{1,2}\\.\\d{1,3}).{0,48}?(?:小|U(?:nder)?)\\s*(?:[0-9]+(?:\\.[0-9]+)?\\s+)?(\\d{1,2}\\.\\d{1,3})/i)
                  || t.match(/(?:小|U(?:nder)?)\\s*([0-9]+(?:\\.[0-9]+)?)\\s+(\\d{1,2}\\.\\d{1,3}).{0,48}?(?:大|O(?:ver)?)\\s*(?:[0-9]+(?:\\.[0-9]+)?\\s+)?(\\d{1,2}\\.\\d{1,3})/i);
                if (ou) {
                  const ln = Number(ou[1]);
                  const a = Number(ou[2]);
                  const b = Number(ou[3]);
                  if (Number.isFinite(ln) && a > 1 && b > 1) {
                    total_line = ln;
                    if (/^(?:小|U)/i.test(ou[0])) { under = a; over = b; }
                    else { over = a; under = b; }
                  }
                } else if (/大|小|Over|Under|\bO\b|\bU\b/i.test(t)) {
                  // compact 常省略「大」：<全场线> <大赔率> 小 <小赔率>。
                  // 先在「小」前后解析紧邻盘口，避免让球 0.5-1 的 1.0 抢走 lineHit。
                  const compact = [...t.matchAll(/(?:^|\\s)(\\d+(?:\\.\\d+)?(?:\\s*-\\s*\\d+(?:\\.\\d+)?)?)\\s+(\\d{1,2}\\.\\d{1,3})\\s*(?:小|Under)\\s*(\\d{1,2}\\.\\d{1,3})(?=$|\\s)/gi)];
                  const compactHit = compact.length ? compact.reduce((best, cur) => {
                    const avg = (String(cur[1]).split('-').map(Number).filter(Number.isFinite));
                    const bestAvg = (String(best[1]).split('-').map(Number).filter(Number.isFinite));
                    const score = avg.length ? avg.reduce((a, b) => a + b, 0) / avg.length : -1;
                    const bestScore = bestAvg.length ? bestAvg.reduce((a, b) => a + b, 0) / bestAvg.length : -1;
                    return score > bestScore ? cur : best;
                  }) : null;
                  if (compactHit) {
                    const compactParts = String(compactHit[1]).split('-').map(Number).filter(Number.isFinite);
                    const compactLine = compactParts.length ? compactParts.reduce((a, b) => a + b, 0) / compactParts.length : NaN;
                    const compactOver = Number(compactHit[2]);
                    const compactUnder = Number(compactHit[3]);
                    if (Number.isFinite(compactLine) && compactOver > 1 && compactUnder > 1) {
                      total_line = compactLine;
                      over = compactOver;
                      under = compactUnder;
                    }
                  }
                  // 盘口线：.0/.25/.5/.75（足球）或 120–280（篮球）；末两档亚赔视作大/小
                  const lineHit = total_line != null ? null : parts.map(Number).find((n) =>
                    Number.isFinite(n) && (
                      (n >= 0.5 && n <= 12 && Math.abs(n * 4 - Math.round(n * 4)) < 1e-6) ||
                      (n >= 120 && n <= 280)
                    )
                  );
                  if (lineHit != null && odds.length >= 2) {
                    total_line = lineHit;
                    over = odds[odds.length - 2];
                    under = odds[odds.length - 1];
                  }
                }

                out.push({
                  league: currentLeague,
                  home,
                  away,
                  odds,
                  home_score: hs,
                  away_score: as_,
                  period,
                  clock,
                  live,
                  sport_hint: sportHint,
                  total_line,
                  over,
                  under,
                  raw: t.slice(0, 220),
                });
                if (out.length >= 250) break;
              }
              return out;
            }"""
                )
                if isinstance(part, list):
                    raw.extend(part)
            except Exception:
                continue
        # 站点插件补充 DOM（平博：正文刮取滚球对阵）
        try:
            from app.services.bookmakers.plugin import get_plugin

            plug = get_plugin(site_code or "")
            raw = await plug.enrich_dom_rows(
                page,
                list(raw or []),
                url_sport=url_sport,
                live_only=live_only,
                on_live_url=on_live_url,
                limit=limit,
            )
        except Exception:
            pass
        if not raw:
            return []
    except Exception as e:
        logger.debug("dom scrape failed: %s", e)
        return []

    profile_name = get_site_profile(site_code).get("name") or site_code
    prefix = (site_code or "site").lower()
    out: list[RemoteMatch] = []
    seen: set[str] = set()
    drop = {
        "junk": 0,
        "odds": 0,
        "not_live": 0,
        "sport": 0,
        "mismatch": 0,
        "league": 0,
    }
    logger.info(
        "dom scrape %s: raw_rows=%d live_only=%s on_live_url=%s url=%s",
        prefix,
        len(raw or []),
        live_only,
        on_live_url,
        (page_url or "")[:120],
    )
    # 抽样：优先 live=true；再补 unique 对阵，便于排查「侧栏有滚球数但 rows=0」
    samples: list[dict] = []
    seen_pair: set[str] = set()
    for sample in raw or []:
        if not isinstance(sample, dict):
            continue
        key = f"{sample.get('home')}|{sample.get('away')}"
        if sample.get("live") or key not in seen_pair:
            seen_pair.add(key)
            samples.append(sample)
        if len(samples) >= 8:
            break
    live_raw = sum(1 for s in (raw or []) if isinstance(s, dict) and s.get("live"))
    logger.info("dom scrape %s: unique_pairs~%d live_raw=%d", prefix, len(seen_pair), live_raw)
    for sample in samples[:5]:
        logger.info(
            "dom sample %s home=%s away=%s live=%s clock=%s period=%s odds=%s raw=%s",
            prefix,
            sample.get("home"),
            sample.get("away"),
            sample.get("live"),
            sample.get("clock"),
            sample.get("period"),
            sample.get("odds"),
            str(sample.get("raw") or "")[:120],
        )

    for item in raw or []:
        if not isinstance(item, dict):
            continue
        home = str(item.get("home") or "").strip()
        away = str(item.get("away") or "").strip()
        if not home or not away:
            continue
        league = str(item.get("league") or "").strip()[:100]
        home, away = _recover_teams_from_league(home, away, league)
        if _is_junk_team_name(home) or _is_junk_team_name(away):
            drop["junk"] += 1
            continue
        odds_vals = [
            eu
            for eu in (coerce_float_european(x) for x in (item.get("odds") or []))
            if eu is not None
        ]
        if len(odds_vals) < 2:
            drop["odds"] += 1
            continue
        is_live = bool(item.get("live"))
        period = str(item.get("period") or "")
        clock = str(item.get("clock") or "")
        hs = int(item.get("home_score") or 0)
        aws = int(item.get("away_score") or 0)
        raw_txt = str(item.get("raw") or "")
        from app.services.bookmakers.match_live import is_actually_started, looks_like_kickoff_score

        if looks_like_kickoff_score(hs, aws) and not period:
            hs, aws = 0, 0
        # 早盘开赛时刻行（即便挂在 /live 页侧栏/混排）一律丢弃
        if _raw_looks_scheduled_early(raw_txt):
            drop["not_live"] += 1
            continue
        # 有真实比分/强节次才补 period；禁止仅靠墙钟伪造「进行中」
        if is_live and not period:
            if (hs > 0 or aws > 0) and not looks_like_kickoff_score(hs, aws):
                period = "进行中"
            elif re.search(r"第[一二三四1-4]\s*节|Q[1-4]|上半场|下半场", raw_txt, re.I):
                mper = re.search(r"(第[一二三四1-4]\s*节|Q[1-4]|上半场|下半场)", raw_txt, re.I)
                period = (mper.group(1) if mper else "进行中")
        if is_live and not is_actually_started(
            status="live",
            period=period,
            clock=clock,
            home_score=hs,
            away_score=aws,
        ):
            is_live = False
        if live_only and not is_live:
            drop["not_live"] += 1
            continue
        if league and (_is_junk_team_name(league) or _UI_JUNK_RE.search(league)):
            league = ""
        # 用队名/联赛/比分/节次/Tab 严格判定球类；无法判定则丢弃（绝不默认足球）
        from app.services.bookmakers.sport_classify import (
            classify_sport,
            is_credible_live_basketball,
            looks_like_basketball_score,
            reject_sport_mismatch,
        )

        hint = item.get("sport_hint") or item.get("forced_sport") or url_sport or ""
        sport = classify_sport(
            text=f"{league} {home} {away} {raw_txt}",
            period=period or str(item.get("period") or ""),
            home_score=hs,
            away_score=aws,
            sport_hint=hint,
        )
        # 比分/节次优先；禁止 URL/hint 把 1-0 伪行标成篮球
        hint_sport = str(item.get("sport_hint") or "").strip().lower()
        if looks_like_basketball_score(hs, aws) or re.search(
            r"(?:Q[1-4]|第[一二三四1-4]\s*节)", f"{period} {raw_txt}", re.I
        ):
            sport = "basketball"
        elif hint_sport == "football" or (
            url_sport == "football" and not looks_like_basketball_score(hs, aws)
        ):
            sport = sport or "football"
        elif hint_sport == "basketball" or url_sport == "basketball":
            if is_credible_live_basketball(
                period=period, clock=clock, home_score=hs, away_score=aws, text=raw_txt
            ):
                sport = "basketball"
            else:
                sport = None  # 篮球页早盘/误刮低分 → 丢弃
        if url_sport == "basketball" and _SCHEDULED_DATE_RE.search(raw_txt) and max(hs, aws) < 20:
            drop["not_live"] += 1
            continue
        if sport == "basketball" and not is_credible_live_basketball(
            period=period, clock=clock, home_score=hs, away_score=aws, text=f"{league} {raw_txt}"
        ):
            drop["mismatch"] += 1
            continue
        if sport not in ("football", "basketball"):
            drop["sport"] += 1
            continue
        # 无真实联赛名丢弃（禁止「足球滚球/篮球滚球」占位写入）
        _ph_lg = {
            "", "未知联赛", "未知", "N/A", "-", "—",
            "足球滚球", "篮球滚球", "滚球", "足球", "篮球", "体育", "今日",
        }
        if (not league) or league in _ph_lg:
            drop["league"] += 1
            continue
        if reject_sport_mismatch(
            sport,
            period=period or str(item.get("period") or ""),
            home_score=hs,
            away_score=aws,
            text=f"{league} {home} {away}",
        ):
            drop["mismatch"] += 1
            continue

        # 独赢：前 2~3 个赔率；让分/大小无法可靠拆分时至少给 moneyline
        ml_data: dict[str, Any] = {"home": odds_vals[0], "away": odds_vals[1] if len(odds_vals) > 1 else odds_vals[0]}
        if sport == "football" and len(odds_vals) >= 3:
            ml_data["draw"] = odds_vals[2]
        odds_list = [
            RemoteOdds(
                bet_type="moneyline",
                odds_data={
                    **ml_data,
                    "_site": {"bet_type": "moneyline", "source": "dom", "site_code": prefix, "sport": sport},
                },
            )
        ]
        # 额外赔率对：尝试 spread（启发式）
        if len(odds_vals) >= 4 and not (item.get("over") and item.get("under")):
            # 尝试从 raw 文本提取让球线（如 -0.5, 1.0, -1.5）
            spread_hc: Optional[float] = None
            hc_match = re.search(r"(-?\d+(?:\.[05])?)\s*(?:让|球|H(?:andicap)?)", raw_txt, re.I)
            if not hc_match:
                # 独立的 -0.5 / 0.5 / -1.0 等模式（OB H5 常见）
                hc_match = re.search(r"(?<![0-9.])(-?\d+\.[05])(?![0-9])", raw_txt)
            if hc_match:
                try:
                    v = float(hc_match.group(1))
                    if sport == "football" and -5 <= v <= 5:
                        spread_hc = v
                    elif sport == "basketball" and -50 <= v <= 50:
                        spread_hc = v
                except (TypeError, ValueError):
                    pass
            odds_list.append(
                RemoteOdds(
                    bet_type="spread",
                    spread=spread_hc if spread_hc is not None else 0,
                    odds_data={
                        "home": odds_vals[-2],
                        "away": odds_vals[-1],
                        "handicap": spread_hc if spread_hc is not None else 0,
                        "_site": {"bet_type": "spread", "source": "dom", "site_code": prefix, "sport": sport},
                    },
                )
            )

        # 全场大小球（DOM 正则 / 关键词兜底）
        over_v = coerce_float_european(item.get("over"))
        under_v = coerce_float_european(item.get("under"))
        try:
            total_line = float(item.get("total_line")) if item.get("total_line") is not None else None
        except (TypeError, ValueError):
            total_line = None
        if (not over_v or not under_v) and item.get("raw"):
            tot = _parse_total_from_raw(str(item.get("raw") or ""), sport=sport)
            if tot:
                # DOM JS 的宽松兜底可能先把相邻让球线（如 0.5-1）
                # 当成 total_line，并留下一个错误 over。正文结构解析更精确：
                # 省略「大」标签时仍能锁定「全场线 + over + 小 + under」，
                # 因此只要命中就整体替换，不能用 `or` 保留旧猜测。
                over_v = tot.get("over") or over_v
                under_v = tot.get("under") or under_v
                total_line = tot.get("line") or total_line
        # 允许只有 under 或只有 over 时也创建 TOTAL
        if total_line and ((under_v and 1.1 <= under_v <= 10) or (over_v and over_v > 1)):
            total_data: dict[str, Any] = {
                "_site": {
                    "bet_type": "total",
                    "source": "dom",
                    "site_code": prefix,
                    "sport": sport,
                    "line": float(total_line),
                },
            }
            if under_v and 1.1 <= under_v <= 10:
                total_data["under"] = float(under_v)
            if over_v and over_v > 1:
                total_data["over"] = float(over_v)
            odds_list.append(
                RemoteOdds(
                    bet_type="total",
                    total=float(total_line),
                    odds_data=total_data,
                )
            )

        # 上下半场大小球解析（从 raw 文本提取"上半场"/"下半场"标识的大小球）
        raw_txt = str(item.get("raw") or "")
        for half_label, half_bt in (("上半场", "first_half_total"), ("下半场", "second_half_total")):
            if half_label in raw_txt:
                ht = _parse_total_from_raw(raw_txt, sport=sport)
                if ht and (ht.get("under") or ht.get("over")):
                    ht_under = ht.get("under")
                    ht_over = ht.get("over")
                    ht_line = ht.get("line")
                    if ht_line and ((ht_under and 1.1 <= ht_under <= 10) or (ht_over and ht_over > 1)):
                        ht_data: dict[str, Any] = {
                            "_site": {
                                "bet_type": half_bt,
                                "source": "dom",
                                "site_code": prefix,
                                "sport": sport,
                                "line": float(ht_line),
                                "period": half_label,
                            },
                        }
                        if ht_under and 1.1 <= ht_under <= 10:
                            ht_data["under"] = float(ht_under)
                        if ht_over and ht_over > 1:
                            ht_data["over"] = float(ht_over)
                        odds_list.append(
                            RemoteOdds(
                                bet_type=half_bt,
                                total=float(ht_line),
                                odds_data=ht_data,
                            )
                        )

        # 赔率去重：同 bet_type + 同盘口线只保留最新一条
        _dedup: dict[str, RemoteOdds] = {}
        for o in odds_list:
            key = f"{o.bet_type}|{o.total or 0}|{o.spread or 0}"
            _dedup[key] = o
        odds_list = list(_dedup.values())

        mid = f"dom|{sport}|{league}|{home}|{away}"
        ext = f"{prefix}:{mid}"
        if ext in seen:
            continue
        seen.add(ext)
        out.append(
            RemoteMatch(
                external_id=ext,
                sport=sport,
                league=league,
                home_team=home,
                away_team=away,
                start_time="",
                status="live" if is_live else "upcoming",
                venue=str(profile_name),
                odds_list=odds_list,
                home_score=hs,
                away_score=aws,
                clock=clock if is_live else "",
                period=period if is_live else "",
            )
        )
        if len(out) >= limit:
            break
    logger.info("dom scrape %s: rows=%d drop=%s", site_code, len(out), drop)
    return out


def _as_odds(v: Any) -> Optional[float]:
    return coerce_float_european(v)


def _normalize_match_odds(m: RemoteMatch) -> RemoteMatch:
    cleaned: list[RemoteOdds] = []
    for o in m.odds_list or []:
        data = normalize_odds_data_to_european(o.odds_data)
        if not any(k for k in data if not str(k).startswith("_")):
            continue
        cleaned.append(
            RemoteOdds(bet_type=o.bet_type, odds_data=data, spread=o.spread, total=o.total)
        )
    m.odds_list = cleaned
    return m


async def fetch_venue_live_odds(
    page,
    *,
    site_code: str,
    base_url: str,
    limit: int = 300,
    live_only: bool = False,
    wait_ms: int = 7000,
) -> list[RemoteMatch]:
    """
    统一入口：进场馆 → 切盘口 Tab → XHR 解析 → DOM 兜底。
    """
    from app.services.bookmakers.site_session import site_sessions
    from app.services.bookmakers.venue_entry import activate_sportsbook_tabs, enter_portal_venue

    profile = get_site_profile(site_code)
    hints = tuple(profile.get("odds_url_hints") or ())
    captured: list[Any] = []
    active = page

    async def on_response(resp):
        try:
            url = (resp.url or "").lower()
            # 宽匹配：场馆内绝大多数盘口 API 都吃进来
            if hints and not any(h.lower() in url for h in hints):
                if not any(
                    x in url
                    for x in (
                        "sport",
                        "odds",
                        "match",
                        "event",
                        "fixture",
                        "league",
                        "market",
                        "handicap",
                        "live",
                        "today",
                        "getlist",
                        "matchlist",
                        "yewu",
                        "mmp",
                    )
                ):
                    return
            ctype = (resp.headers.get("content-type") or "").lower()
            if "json" not in ctype and "javascript" not in ctype and "text" not in ctype:
                # 仍尝试 json()
                pass
            try:
                body = await resp.json()
            except Exception:
                return
            if body is not None:
                captured.append(body)
        except Exception:
            return

    page.on("response", on_response)
    base = (base_url or "").rstrip("/")
    dom_forced: list[RemoteMatch] = []
    try:
        from app.services.bookmakers.site_profiles import needs_manual_venue
        from app.services.bookmakers.venue_entry import (
            is_in_sportsbook as _in_book_now,
            page_is_off_match_list,
            recover_pinnacle_live_list,
        )

        code0 = (site_code or "").lower()
        # 平博：搜索页 / 早盘 sports 列表都先拉回滚球盘，再采（live_only 必做）
        if code0 == "pinnacle":
            try:
                from app.services.bookmakers.venue_entry import page_already_on_live_board as _on_live

                need_recover = await page_is_off_match_list(page)
                if not need_recover and live_only:
                    need_recover = not await _on_live(page)
                if need_recover:
                    await recover_pinnacle_live_list(page)
            except Exception as e:
                logger.warning("pinnacle pre-recover failed: %s", e)

        # OB 人工进馆站：禁止自动乱点（易点错回首页）；已在盘口则跳过
        already_in = False
        try:
            already_in = await _in_book_now(page)
        except Exception:
            already_in = False
        if not already_in and not needs_manual_venue(site_code):
            active, _ = await enter_portal_venue(
                page,
                site_code=site_code,
                base_url=base,
                context=getattr(page, "context", None),
                timeout_ms=8000,
                force=False,
                wait_manual=False,
            )
        else:
            active = page
            if not already_in and needs_manual_venue(site_code):
                logger.info(
                    "venue live %s: skip auto-click (manual venue); stay on current page",
                    site_code,
                )
        from app.services.bookmakers.venue_entry import is_in_sportsbook, _is_venue_url

        if not await is_in_sportsbook(active):
            # 手动场馆站禁止 goto 旧 venue_url（易触发系统错误/回首页）
            if not needs_manual_venue(site_code):
                dest = ""
                try:
                    sess = site_sessions.get(base)
                    if sess:
                        dest = str(sess.venue_url or "").strip()
                except Exception:
                    dest = ""
                if dest and _is_venue_url(dest):
                    try:
                        logger.info("venue live %s: restore venue_url for live data", site_code)
                        await active.goto(dest, wait_until="domcontentloaded", timeout=45000)
                        await active.wait_for_timeout(1200)
                    except Exception as e:
                        logger.warning("venue live restore failed: %s", e)
            if not await is_in_sportsbook(active):
                if code0 == "pinnacle":
                    try:
                        if await recover_pinnacle_live_list(active) and await is_in_sportsbook(active):
                            logger.info("venue live pinnacle: recovered into sportsbook")
                        else:
                            logger.warning(
                                "venue live skip pinnacle: not in sportsbook after recover"
                            )
                            return []
                    except Exception as e:
                        logger.warning("venue live pinnacle recover failed: %s", e)
                        return []
                else:
                    logger.warning(
                        "venue live skip %s: session not in sportsbook (manual entry required on verify)",
                        site_code,
                    )
                    return []
        if active is not page:
            try:
                await site_sessions.update_page(base, active)
            except Exception:
                pass
            try:
                active.on("response", on_response)
            except Exception:
                pass

        from app.services.bookmakers.venue_entry import (
            dismiss_blocking_modals,
            page_has_system_error,
            page_has_trade_password_modal,
        )

        # 交易密码 / 系统错误：立刻停手，不要再 dismiss/点 Tab（否则会踢回首页）
        try:
            if await page_has_trade_password_modal(active):
                logger.warning(
                    "venue live skip %s: trade-password modal open — leave page alone",
                    site_code,
                )
                return []
            if await page_has_system_error(active):
                logger.warning(
                    "venue live skip %s: system-error toast — leave page alone",
                    site_code,
                )
                return []
        except Exception:
            pass

        await dismiss_blocking_modals(active)
        from app.services.bookmakers.venue_entry import page_already_on_live_board

        code = (site_code or "").lower()
        # 仅「已在滚球盘」才 stay_put；早盘 sports 列表仍允许 gentle 点一次滚球
        stay_put = False
        try:
            stay_put = await page_already_on_live_board(active)
        except Exception:
            stay_put = False
        # OB 已在场馆 H5（非大厅）也可静默采，避免乱点回综合站
        if not stay_put and code in ("ob", "ybty", "kaiyun"):
            try:
                stay_put = await is_in_sportsbook(active)
            except Exception:
                stay_put = False
        gentle = (
            stay_put
            or needs_manual_venue(site_code)
            or code in ("ob", "pinnacle", "ybty", "kaiyun")
        )
        try:
            cur = (active.url or "")[:120]
        except Exception:
            cur = ""
        if stay_put:
            logger.info(
                "venue live %s stay on live board (no goto/tabs) url=%s",
                site_code,
                cur,
            )
        else:
            await activate_sportsbook_tabs(active, live_only=live_only, gentle=gentle)
            try:
                if await page_has_trade_password_modal(active) or await page_has_system_error(active):
                    logger.warning("venue live abort %s after tab switch: modal/error", site_code)
                    return []
            except Exception:
                pass
            await dismiss_blocking_modals(active)
        soft_capture = stay_put or gentle
        await capture_venue_payloads(active, captured, wait_ms=wait_ms, soft=soft_capture)

        # 分球类再采。平博：已在 /live 只刮当前页（SPA 自更新比分），禁止 goto/reload 白屏。
        # 偏离滚球时最多恢复到一个 live URL，不再每轮轮询足球+篮球双跳。
        from app.services.bookmakers.plugin import get_plugin
        from app.services.bookmakers.sport_classify import looks_like_basketball_score

        sport_tabs = (("足球", "football"), ("篮球", "basketball"))
        cur_url_l = ""
        try:
            cur_url_l = (active.url or "").lower()
        except Exception:
            cur_url_l = ""
        if "basket" in cur_url_l:
            url_forced = "basketball"
        elif "soccer" in cur_url_l or "/football" in cur_url_l:
            url_forced = "football"
        else:
            url_forced = ""

        if code == "pinnacle" and live_only:
            # /sports/basketball（无 /live）也算已在球类页：先采当前，再 dual-live 补另一球
            on_sport_page = bool(url_forced) and (
                "/live" in cur_url_l
                or "/sports/soccer" in cur_url_l
                or "/sports/football" in cur_url_l
                or "/sports/basketball" in cur_url_l
            )
            if stay_put or on_sport_page or "/live" in cur_url_l:
                sport_tabs = (("__current__", url_forced),)
                logger.info(
                    "venue live pinnacle no-nav scrape url=%s sport=%s",
                    cur_url_l[:120],
                    url_forced or "auto",
                )
            else:
                # 偏离盘口：按当前偏好恢复到对应 /live（勿永远跳足球）
                try:
                    urls = get_plugin(code).live_sport_urls(active.url or "")
                except Exception:
                    urls = []
                dest = ""
                prefer = url_forced or "football"
                for u in urls or []:
                    ul = u.lower()
                    if prefer == "basketball" and "basketball" in ul and "/live" in ul:
                        dest = u
                        break
                    if prefer == "football" and ("soccer" in ul or "football" in ul) and "/live" in ul:
                        dest = u
                        break
                if not dest and urls:
                    dest = urls[0]
                if dest:
                    sport_tabs = ((f"__url__:{dest}", prefer if prefer in ("football", "basketball") else "football"),)
                else:
                    sport_tabs = (("__current__", url_forced),)
        elif stay_put or gentle:
            sport_tabs = (("__current__", url_forced or ""),)

        for label, forced in sport_tabs:
            try:
                if label.startswith("__url__:"):
                    dest = label.split(":", 1)[1]
                    if await page_has_trade_password_modal(active) or await page_has_system_error(active):
                        break
                    # 平博：仅偏离盘口时允许一次 goto；禁止 reload
                    try:
                        await active.goto(dest, wait_until="domcontentloaded", timeout=45000)
                        await active.wait_for_timeout(1000)
                        logger.info("venue live pinnacle open once %s", dest[:120])
                    except Exception as e:
                        logger.warning("venue live pinnacle goto failed %s: %s", dest[:80], e)
                        continue
                elif label != "__current__":
                    if await page_has_trade_password_modal(active) or await page_has_system_error(active):
                        break
                    loc = active.get_by_text(label, exact=True).first
                    if await loc.count() == 0:
                        loc = active.get_by_text(label, exact=False).first
                    if await loc.count() > 0:
                        await loc.click(timeout=2500)
                        await active.wait_for_timeout(800)
                        if live_only:
                            for text in ("滚球", "滚球盘", "Live", "In-Play"):
                                try:
                                    tloc = active.get_by_text(text, exact=False).first
                                    if await tloc.count() > 0:
                                        await tloc.click(timeout=1500)
                                        await active.wait_for_timeout(500)
                                        break
                                except Exception:
                                    continue
                await capture_venue_payloads(
                    active, captured, wait_ms=max(2000, wait_ms // 3), soft=soft_capture
                )
                # 平博：禁止乱点「足球/比赛/今天/早盘」（会把滚球切成早盘 600+）；球类只靠 /live URL
                if code == "pinnacle":
                    try:
                        # 仅点侧栏「滚球盘」巩固；不要点「足球」（易点到早盘 1044）
                        try:
                            if "/live" not in ((active.url or "").lower()):
                                loc = active.get_by_text("滚球盘", exact=True).first
                                if await loc.count() > 0:
                                    await loc.click(timeout=1500)
                                    await active.wait_for_timeout(600)
                        except Exception:
                            pass
                        marks = await active.evaluate(
                            """() => {
                              const t = ((document.body && document.body.innerText) || '').replace(/\\s+/g, ' ');
                              const liveSide = (t.match(/滚球盘[^\\d]{0,20}足球\\s*(\\d+)/) || [])[1] || '';
                              const liveBb = (t.match(/滚球盘[^\\d]{0,40}篮球\\s*(\\d+)/) || [])[1] || '';
                              const hiScores = t.match(/\\b(?:[2-9]\\d|[1-9]\\d{2})\\s*[-:]\\s*(?:[2-9]\\d|[1-9]\\d{2})\\b/g) || [];
                              const apostrophe = t.match(/\\b\\d{1,3}['′]/g) || [];
                              const halves = t.match(/上半场|下半场/g) || [];
                              // 截取「比赛 N」后主列表，看是否真有滚球行
                              const idx = t.search(/比赛\\s*\\d+/);
                              const board = idx >= 0 ? t.slice(idx, idx + 700) : '';
                              const vsLiveish = [];
                              const re = /([\\u4e00-\\u9fffA-Za-z][\\u4e00-\\u9fffA-Za-z0-9.\\-·]{1,23})\\s+vs\\s+([\\u4e00-\\u9fffA-Za-z][\\u4e00-\\u9fffA-Za-z0-9.\\-·]{1,23})(.{0,80})/gi;
                              let m;
                              while ((m = re.exec(t)) && vsLiveish.length < 10) {
                                const tail = m[3] || '';
                                if (/\\d{1,2}\\s*月\\s*\\d{4}|未开始/.test(tail)) continue;
                                if (/\\d{1,3}['′]|上半场|下半场|\\b(?:1H|2H|HT)\\b/i.test(tail)) {
                                  vsLiveish.push((m[1] + ' vs ' + m[2] + ' | ' + tail).replace(/\\s+/g, ' ').slice(0, 110));
                                }
                              }
                              // 撇号分钟前后窗，便于核对真实滚球排版
                              const clockWins = [];
                              const reC = /\\b\\d{1,3}['′]/g;
                              while ((m = reC.exec(t)) && clockWins.length < 8) {
                                clockWins.push(t.slice(Math.max(0, m.index - 70), m.index + 50).replace(/\\s+/g, ' '));
                              }
                              return {
                                len: t.length,
                                liveSide,
                                liveBb,
                                hiScores: hiScores.slice(0, 8),
                                apostrophe: apostrophe.slice(0, 12),
                                halves: halves.length,
                                vsLiveish,
                                clockWins,
                                board: board.slice(0, 400),
                              };
                            }"""
                        )
                        logger.info(
                            "venue live pinnacle page marks url=%s marks=%s",
                            (active.url or "")[:100],
                            marks,
                        )
                    except Exception:
                        pass
                rows = await scrape_dom_matches(
                    active, site_code=site_code, live_only=live_only, limit=limit
                )
                for m in rows:
                    # 严格分类：禁止用 Tab 强行改写球类；未识别或与当前 Tab 冲突则丢弃
                    if m.sport not in ("football", "basketball"):
                        continue
                    # 足球 live 页可附带篮球滚球（篮球 /live 维护时兜底）；其余强制球类一致
                    if forced and m.sport != forced:
                        if not (forced == "football" and m.sport == "basketball"):
                            continue
                    if (
                        forced == "football"
                        and m.sport == "football"
                        and looks_like_basketball_score(m.home_score, m.away_score)
                    ):
                        continue
                    if str(getattr(m, "status", "") or "").lower() != "live":
                        continue
                    dom_forced.append(m)
            except Exception:
                logger.debug("per-sport capture failed site=%s sport=%s", site_code, forced, exc_info=True)

        # 平博：仅在当前页缺另一球类时补采；冷却/超时缩短以加速轮询
        if code == "pinnacle" and live_only:
            have = {
                str(getattr(m, "sport", "") or "").lower()
                for m in dom_forced
            }
            have_fb = bool(have & {"football", "soccer"})
            have_bk = "basketball" in have
            if have_fb and have_bk:
                logger.info(
                    "venue live pinnacle skip dual-live: both sports already present (%d)",
                    len(dom_forced),
                )
            else:
                try:
                    import asyncio

                    from app.services.bookmakers.plugins.pinnacle.dual_live import (
                        scrape_other_live_sport,
                    )

                    sibling = await asyncio.wait_for(
                        scrape_other_live_sport(active, limit=limit),
                        timeout=32.0,
                    )
                    seen_dom = {m.external_id for m in dom_forced}
                    for m in sibling or []:
                        if m.external_id in seen_dom:
                            continue
                        dom_forced.append(m)
                        seen_dom.add(m.external_id)
                    logger.info(
                        "venue live pinnacle dual-live merged +%d",
                        len(sibling or []),
                    )
                except asyncio.TimeoutError:
                    logger.warning("venue live pinnacle dual-live timeout")
                except Exception as e:
                    logger.warning("venue live pinnacle dual-live failed: %s", e)
    finally:
        for p in {page, active}:
            try:
                p.remove_listener("response", on_response)
            except Exception:
                pass

    parsed = parse_captured_payloads(
        captured,
        site_code=site_code,
        limit=limit,
        live_only=live_only,
    )
    seen = {m.external_id for m in parsed}
    for m in dom_forced:
        if m.external_id not in seen:
            parsed.append(m)
            seen.add(m.external_id)
        if len(parsed) >= limit:
            break
    if len(parsed) < 5:
        dom_rows = await scrape_dom_matches(active, site_code=site_code, live_only=live_only, limit=limit)
        for m in dom_rows:
            if m.external_id not in seen:
                parsed.append(m)
                seen.add(m.external_id)
            if len(parsed) >= limit:
                break

    # 平博/OB 采空：已在盘口则就地再刮；偏离时走插件 after_empty（禁止无脑 recreate）
    code_l = (site_code or "").lower()
    if code_l in ("pinnacle", "ob", "ybty", "kaiyun") and len(parsed) == 0:
        try:
            from app.services.bookmakers.plugin import get_plugin
            from app.services.bookmakers.venue_entry import page_already_on_live_board

            on_live = False
            try:
                on_live = await page_already_on_live_board(active) or (
                    "/live" in ((active.url or "").lower())
                )
            except Exception:
                on_live = "/live" in ((active.url or "").lower())
            if code_l in ("ob", "ybty", "kaiyun"):
                try:
                    from app.services.bookmakers.plugins.ob.venue import page_in_ob_venue

                    on_live = on_live or await page_in_ob_venue(active)
                except Exception:
                    pass
            if not on_live:
                await get_plugin(code_l if code_l in ("pinnacle", "ob") else "ob").after_empty_odds(
                    active
                )
            await capture_venue_payloads(
                active, captured, wait_ms=max(2000, wait_ms // 2), soft=True
            )
            parsed = parse_captured_payloads(
                captured,
                site_code=site_code,
                limit=limit,
                live_only=live_only,
            )
            seen = {m.external_id for m in parsed}
            for m in await scrape_dom_matches(
                active, site_code=site_code, live_only=live_only, limit=limit
            ):
                if m.external_id not in seen:
                    parsed.append(m)
                    seen.add(m.external_id)
                if len(parsed) >= limit:
                    break
            logger.info(
                "venue live %s empty-retry on_live=%s parsed=%d",
                code_l,
                on_live,
                len(parsed),
            )
        except Exception as e:
            logger.warning("venue live %s empty-retry failed: %s", code_l, e)

    logger.info(
        "venue live %s: xhr_payloads=%d parsed=%d live_only=%s",
        site_code,
        len(captured),
        len(parsed),
        live_only,
    )
    return [_normalize_match_odds(m) for m in parsed[:limit]]


# 兼容：平博正文刮取已迁至 plugins.pinnacle.live_text
from app.services.bookmakers.plugins.pinnacle.live_text import (  # noqa: E402,F401
    scrape_pinnacle_live_text,
)
