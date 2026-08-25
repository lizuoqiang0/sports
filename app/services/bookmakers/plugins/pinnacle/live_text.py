"""平博滚球正文刮取。"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_PLACEHOLDER_LEAGUES = frozenset({"足球滚球", "篮球滚球", "滚球", "足球", "篮球", "体育", "今日"})
_LEAGUE_LIMIT_NOTICE_RE = re.compile(
    r"[\s\u200e\u200f]*(?:您)?已达到(?:选择的)?比赛数量上限[\s\u200e\u200f]*\d*[\s\u200e\u200f]*场?[\s\u200e\u200f]*",
    re.IGNORECASE,
)


def clean_pinnacle_league(value: Any) -> str:
    """移除平博列表插入的收藏/选择数量提示，保留真实联赛名。"""
    league = " ".join(str(value or "").split())
    league = _LEAGUE_LIMIT_NOTICE_RE.sub(" ", league)
    return league.strip(" -–—")[:80]


async def scrape_pinnacle_live_text(page, *, url_sport: str = "", limit: int = 80) -> list[dict]:
    """从整页文本抓滚球对阵。

    平博紧凑滚球排版（实测）：
      联赛  0-0  2H  13'  主队‎ 客队‎  盘口 赔率...
      联赛  51-78  Q3  3'  主队‎ 客队‎  ...
    队名在比分/节次/分钟之后，常用 LTR(\\u200e) 分隔；联赛在比分之前。
    """
    try:
        from app.services.bookmakers.plugins.pinnacle.modals import (
            dismiss_pinnacle_blocking_modals,
        )

        await dismiss_pinnacle_blocking_modals(page)
    except Exception:
        pass

    try:
        rows = await page.evaluate(
            """(sportHint) => {
              const rawBody = (document.body && document.body.innerText) || '';
              const t = rawBody.replace(/\\u00a0/g, ' ').replace(/[ \\t\\r\\n]+/g, ' ');
              const tFlat = t.replace(/[\\u200e\\u200f]/g, ' ').replace(/\\s+/g, ' ');
              const out = [];
              const seen = new Set();
              const junk = /投注|结算|滚球|今日|串关|登录|公告|输赢|独赢|让分|大小|未开始|即将开始|挪威|瑞典|丹麦|芬兰|^vs$|^比赛$|^体育$|^足球$|^篮球$|^(?:[12]H|HT|FT|OT|ET|Q[1-4]|PEN|AH|H1|H2)$|^\\d+(?:\\.\\d+)?$/i;
              const isName = (s) => {
                const n = (s || '').replace(/[\\u200e\\u200f]/g, '').trim();
                if (n.length < 2 || n.length > 40) return false;
                if (junk.test(n)) return false;
                if (/^\\d/.test(n) || /^\\d+(?:\\.\\d+)?$/.test(n)) return false;
                if (/月|GMT|UTC|AM|PM|小$|大$/i.test(n)) return false;
                if (!/[\\u4e00-\\u9fffA-Za-z]/.test(n)) return false;
                return true;
              };
              const isPlaceholderLeague = (lg) => {
                const x = (lg || '').trim();
                return !x || /^(足球滚球|篮球滚球|滚球|足球|篮球|体育|今日)$/.test(x);
              };
              // 比分前取联赛：排版为「…联赛名 0-0 2H 13' 主‎客‎」
              // 注意切开上一场残留的「队名 + 赔率」，只留紧贴本场比分的联赛段
              const leagueBefore = (full, idx) => {
                let s = full.slice(Math.max(0, idx - 120), idx)
                  .replace(/[\\u200e\\u200f]/g, ' ')
                  .replace(/\\s+/g, ' ')
                  .trim();
                if (!s) return '';
                // 按「连续亚赔」切开，联赛通常在最后一段
                const segs = s.split(/(?:\\s+\\d+\\.\\d{2,3}){2,}/);
                s = (segs[segs.length - 1] || '').trim();
                s = s.replace(/(?:\\s+-?\\d+\\.\\d{1,3})+$/g, '').trim();
                s = s.replace(/(?:\\s+(?:大|小|O|U|主|客|平))+$/i, '').trim();
                if (!s) return '';
                // 取含联赛/杯关键字的最短尾段（避免吞入上一场队名）
                let m = s.match(/((?:[\\u4e00-\\u9fffA-Za-z0-9.\\-·&/]+[\\s\\-]*){0,4}(?:联赛|杯赛|锦标赛|超级联赛|甲级联赛|乙级联赛|丙级联赛|女子联赛|青年联赛|League|Cup|Serie\\s*[ABC]?|Championship|Tournament|Division)(?:[\\s\\-]*[\\u4e00-\\u9fffA-Za-z0-9.\\-·&/]+){0,2})\\s*$/i);
                if (!m) {
                  // 无关键字：仅当末段很短且不像两队名时采用
                  const words = s.split(/\\s+/).filter(Boolean);
                  if (words.length >= 1 && words.length <= 4 && words.join('').length <= 28) {
                    m = [null, words.join(' ')];
                  }
                }
                if (!m) return '';
                let lg = String(m[1] || '').trim().replace(/^[\\-–—\\s]+|[\\-–—\\s]+$/g, '');
                // 平博会把「您已达到选择的比赛数量上限200场」插在联赛标题后。
                lg = lg.replace(/[\\s\\u200e\\u200f]*(?:您)?已达到(?:选择的)?比赛数量上限[\\s\\u200e\\u200f]*\\d*[\\s\\u200e\\u200f]*场?[\\s\\u200e\\u200f]*/g, ' ').trim();
                if (lg.length < 2 || lg.length > 48) return '';
                if (/^(足球|篮球|滚球|体育|今日|比赛)$/.test(lg)) return '';
                // 拒绝「队A 队B 某某联赛」——若空格分段过多且前段不像地区前缀则截到联赛关键字起
                const kw = lg.search(/(?:联赛|杯赛|锦标赛|超级联赛|League|Cup)/i);
                if (kw > 12) {
                  const prefix = lg.slice(0, kw).trim();
                  // 前缀过长多半是残留队名
                  if (prefix.split(/\\s+/).length >= 2) lg = lg.slice(Math.max(0, kw - 10)).trim();
                }
                if (junk.test(lg) && !/(?:联赛|杯|League|Cup)/i.test(lg)) return '';
                return lg.slice(0, 80);
              };
              const pushRow = (row) => {
                const key = (row.sport_hint || '') + '|' + row.home + '|' + row.away;
                const prev = out.find((r) => (r.sport_hint || '') + '|' + r.home + '|' + r.away === key);
                if (prev) {
                  // 同场：保留更好的联赛名 / 更新比分时钟
                  if (isPlaceholderLeague(prev.league) && !isPlaceholderLeague(row.league)) {
                    prev.league = row.league;
                  }
                  if (row.home_score != null) prev.home_score = row.home_score;
                  if (row.away_score != null) prev.away_score = row.away_score;
                  if (row.period) prev.period = row.period;
                  if (row.clock) prev.clock = row.clock;
                  if ((row.odds || []).length > (prev.odds || []).length) prev.odds = row.odds;
                  return;
                }
                if (seen.has(key)) return;
                seen.add(key);
                out.push(row);
              };
              const oddsFrom = (ctx) => (String(ctx || '').match(/(?<![0-9])(\\d\\.\\d{2,3})(?![0-9])/g) || [])
                .map(Number).filter((n) => n > 1 && n < 50);
              const totalFrom = (ctx) => {
                const text = String(ctx || '').replace(/[\\u200e\\u200f]/g, ' ');
                // 格式: <line> <over_odds> 小/Under <under_odds>
                const reUnder = /(?:^|\\s)(\\d+(?:\\.\\d+)?(?:\\s*-\\s*\\d+(?:\\.\\d+)?)?)\\s+(\\d\\.\\d{2,3})\\s*(?:小|Under)\\s*(\\d\\.\\d{2,3})/gi;
                let hit, last = null;
                while ((hit = reUnder.exec(text))) last = hit;
                if (last) {
                  const parts = String(last[1]).split('-').map((x) => Number(String(x).trim()));
                  const numbers = parts.filter((x) => Number.isFinite(x));
                  const line = numbers.length ? numbers.reduce((a, b) => a + b, 0) / numbers.length : NaN;
                  const over = Number(last[2]);
                  const under = Number(last[3]);
                  if (Number.isFinite(line) && line > 0 && Number.isFinite(under) && under > 1)
                    return { line, under, over: Number.isFinite(over) && over > 1 ? over : null };
                }
                // 兜底: <line> <under_odds> 大/Over <over_odds>
                const reOver = /(?:^|\\s)(\\d+(?:\\.\\d+)?(?:\\s*-\\s*\\d+(?:\\.\\d+)?)?)\\s+(\\d\\.\\d{2,3})\\s*(?:大|Over)\\s*(\\d\\.\\d{2,3})/gi;
                let hit2, last2 = null;
                while ((hit2 = reOver.exec(text))) last2 = hit2;
                if (last2) {
                  const parts = String(last2[1]).split('-').map((x) => Number(String(x).trim()));
                  const numbers = parts.filter((x) => Number.isFinite(x));
                  const line = numbers.length ? numbers.reduce((a, b) => a + b, 0) / numbers.length : NaN;
                  const under = Number(last2[2]);
                  const over = Number(last2[3]);
                  if (Number.isFinite(line) && line > 0 && Number.isFinite(over) && over > 1)
                    return { line, under: Number.isFinite(under) && under > 1 ? under : null, over };
                }
                // 兜底2: 仅匹配 <line> 小/Under <under_odds>（无 over）
                const reUnderOnly = /(?:^|\\s)(\\d+(?:\\.\\d+)?(?:\\s*-\\s*\\d+(?:\\.\\d+)?)?)\\s*(?:小|Under)\\s*(\\d\\.\\d{2,3})/gi;
                let hit3, last3 = null;
                while ((hit3 = reUnderOnly.exec(text))) last3 = hit3;
                if (last3) {
                  const parts = String(last3[1]).split('-').map((x) => Number(String(x).trim()));
                  const numbers = parts.filter((x) => Number.isFinite(x));
                  const line = numbers.length ? numbers.reduce((a, b) => a + b, 0) / numbers.length : NaN;
                  const under = Number(last3[2]);
                  if (Number.isFinite(line) && line > 0 && Number.isFinite(under) && under > 1)
                    return { line, under, over: null };
                }
                // 兜底3: 仅匹配 <line> 大/Over <over_odds>（无 under）
                const reOverOnly = /(?:^|\\s)(\\d+(?:\\.\\d+)?(?:\\s*-\\s*\\d+(?:\\.\\d+)?)?)\\s*(?:大|Over)\\s*(\\d\\.\\d{2,3})/gi;
                let hit4, last4 = null;
                while ((hit4 = reOverOnly.exec(text))) last4 = hit4;
                if (last4) {
                  const parts = String(last4[1]).split('-').map((x) => Number(String(x).trim()));
                  const numbers = parts.filter((x) => Number.isFinite(x));
                  const line = numbers.length ? numbers.reduce((a, b) => a + b, 0) / numbers.length : NaN;
                  const over = Number(last4[2]);
                  if (Number.isFinite(line) && line > 0 && Number.isFinite(over) && over > 1)
                    return { line, under: null, over };
                }
                return null;
              };

              // 主通道：比分 + 节次 + 分钟' + 主队‎客队‎
              const rePin = /(\\d{1,3})\\s*[-:：]\\s*(\\d{1,3})\\s+(上半场|下半场|中场|加时|1H|2H|HT|Q[1-4]|第[一二三四1-4]\\s*节)\\s+(\\d{1,3})['′]\\s+([^\\u200e]{2,40})\\u200e\\s*([^\\u200e]{2,40})\\u200e?/g;
              let m;
              while ((m = rePin.exec(t)) && out.length < 160) {
                const hs = Number(m[1]), as_ = Number(m[2]);
                const period = m[3];
                const clock = m[4] + \"'\";
                const home = (m[5] || '').replace(/[\\u200e\\u200f]/g, '').trim();
                const away = (m[6] || '').replace(/[\\u200e\\u200f]/g, '').trim();
                if (!isName(home) || !isName(away) || home === away) continue;
                const tail = t.slice(m.index + m[0].length, m.index + m[0].length + 100).replace(/[\\u200e\\u200f]/g, ' ');
                if (/\\d{1,2}\\s*月\\s*\\d{4}|未开始|即将开始|,\\s*\\d{2}:\\d{2}:\\d{2}/.test(tail.slice(0, 40))) continue;
                const odds = oddsFrom(tail);
                if (odds.length < 2) continue;
                const total = totalFrom(tail);
                const isBb = hs >= 20 || as_ >= 20 || hs + as_ >= 40 || /Q[1-4]|第.+节/i.test(period);
                if (sportHint === 'basketball' && !isBb) continue;
                const league = leagueBefore(t, m.index) || '';
                if (!league || /^(足球滚球|篮球滚球|滚球|足球|篮球|体育|今日)$/.test(league)) continue;
                pushRow({
                  league,
                  home, away, odds: odds.slice(0, 6),
                  under: total && total.under,
                  over: total && total.over,
                  total_line: total && total.line,
                  home_score: hs, away_score: as_,
                  period: period || '进行中',
                  clock, live: true, sport_hint: isBb ? 'basketball' : 'football',
                  raw: (league + ' ' + home + ' vs ' + away + ' ' + hs + '-' + as_ + ' ' + period + ' ' + clock).slice(0, 180),
                });
              }

              // 兜底：去标记后「比分 节次 分钟' 主 客」
              const reFlat = /(\\d{1,3})\\s*[-:：]\\s*(\\d{1,3})\\s+(上半场|下半场|中场|加时|1H|2H|HT|Q[1-4]|第[一二三四1-4]\\s*节)\\s+(\\d{1,3})['′]\\s+([\\u4e00-\\u9fffA-Za-z][\\u4e00-\\u9fffA-Za-z0-9.\\-·&' ]{1,36}?)\\s+([\\u4e00-\\u9fffA-Za-z][\\u4e00-\\u9fffA-Za-z0-9.\\-·&' ]{1,36}?)(?=\\s+(?:\\d+(?:\\.\\d+)?(?:\\s*-\\s*\\d+(?:\\.\\d+)?)?|\\d\\.\\d{2,3}))/g;
              while ((m = reFlat.exec(tFlat)) && out.length < 160) {
                const hs = Number(m[1]), as_ = Number(m[2]);
                const period = m[3];
                const clock = m[4] + \"'\";
                let home = (m[5] || '').trim(), away = (m[6] || '').trim();
                home = home.replace(/\\s+\\d+(?:\\.\\d+)?$/, '').trim();
                away = away.replace(/\\s+\\d+(?:\\.\\d+)?$/, '').trim();
                if (!isName(home) || !isName(away) || home === away) continue;
                const odds = oddsFrom(tFlat.slice(m.index + m[0].length, m.index + m[0].length + 80));
                if (odds.length < 2) continue;
                const total = totalFrom(tFlat.slice(m.index + m[0].length, m.index + m[0].length + 100));
                const isBb = hs >= 20 || as_ >= 20 || hs + as_ >= 40 || /Q[1-4]|第.+节/i.test(period);
                if (sportHint === 'basketball' && !isBb) continue;
                const league = leagueBefore(tFlat, m.index) || '';
                if (!league || /^(足球滚球|篮球滚球|滚球|足球|篮球|体育|今日)$/.test(league)) continue;
                pushRow({
                  league,
                  home, away, odds: odds.slice(0, 6),
                  under: total && total.under,
                  over: total && total.over,
                  total_line: total && total.line,
                  home_score: hs, away_score: as_,
                  period: period || '进行中',
                  clock, live: true, sport_hint: isBb ? 'basketball' : 'football',
                  raw: (league + ' ' + home + ' vs ' + away + ' ' + hs + '-' + as_ + ' ' + period + ' ' + clock).slice(0, 180),
                });
              }
              return out;
            }""",
            url_sport or "",
        )
        clean_rows = []
        for row in list(rows or []):
            if not isinstance(row, dict):
                continue
            normalized = dict(row)
            normalized["league"] = clean_pinnacle_league(normalized.get("league"))
            if normalized["league"]:
                clean_rows.append(normalized)
            if len(clean_rows) >= limit:
                break
        rows = clean_rows
        if rows:
            real_lg = sum(
                1
                for s in rows
                if str(s.get("league") or "").strip() not in _PLACEHOLDER_LEAGUES
            )
            logger.info(
                "pinnacle text scrape: %d rows (%d with real league) url_sport=%s",
                len(rows),
                real_lg,
                url_sport,
            )
            for s in rows[:6]:
                logger.info(
                    "pinnacle text sample league=%s sport=%s %s vs %s %s-%s p=%s c=%s",
                    s.get("league"),
                    s.get("sport_hint"),
                    s.get("home"),
                    s.get("away"),
                    s.get("home_score"),
                    s.get("away_score"),
                    s.get("period"),
                    s.get("clock"),
                )
        return rows
    except Exception as e:
        logger.debug("pinnacle live text scrape failed: %s", e)
        return []
