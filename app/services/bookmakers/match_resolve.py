"""队名规范化、跨站同场匹配与 Match 解析。"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.user import Match, MatchStatus, SportType

logger = logging.getLogger(__name__)

def _norm_team(name: str) -> str:
    """队名规范化，便于跨站同场合并（去空白/双向标记/常见后缀）。"""
    import re

    s = (name or "").strip().lower()
    s = s.replace("\u200e", "").replace("\u200f", "").replace("\xa0", " ")
    s = re.sub(r"\s+", "", s)
    # 去括号内容：(女) -> 空（后续用 suffix 统一处理）
    s = re.sub(r"[\(（][^\)）]*[\)）]", "", s)
    # 统一女足/男足/青年后缀 -> 去掉（两队可能一个带"女足"一个带"(女)"）
    for suf in ("女足", "男足", "青年", "后备", "预备队", "二队"):
        if s.endswith(suf) and len(s) > len(suf) + 1:
            s = s[: -len(suf)]
    # B队/2队/3队 等编号后缀：B队→去掉，2队→去掉
    s = re.sub(r"b队$", "", s)
    s = re.sub(r"[23]队$", "", s)
    # 去掉尾部的"中"字（捷报比分用"切尔西中"表示中场/青年，OB 用"切尔西"）
    if s.endswith("中") and len(s) > 2:
        s = s[:-1]
    for suf in ("足球俱乐部", "足球队", "俱乐部", "fc", "cf", "sc", "afc", "队"):
        if s.endswith(suf) and len(s) > len(suf) + 1:
            s = s[: -len(suf)]
    # 去掉尾部年份后缀：帕尔马1913 → 帕尔马，慕尼黑1860 → 慕尼黑
    s = re.sub(r"\d{2,4}$", "", s)
    # 去掉常见前缀噪声
    for pref in ("pec", "fc", "afc", "sc"):
        if s.startswith(pref) and len(s) > len(pref) + 1:
            s = s[len(pref) :]
    # 去掉常见地名前缀（里约热内卢州、布加勒斯特等）仅当剩余部分 >= 2 字时
    for pref in ("里约热内卢州", "布加勒斯特", "布格勒斯特"):
        if s.startswith(pref) and len(s) > len(pref) + 1:
            s = s[len(pref) :]
            break
    return s


_UI_JUNK_TEAM_RE = None


def _is_junk_team_name(name: str) -> bool:
    """过滤导航/公告/节次类假队名，避免污染跨站匹配。"""
    import re

    global _UI_JUNK_TEAM_RE
    if _UI_JUNK_TEAM_RE is None:
        _UI_JUNK_TEAM_RE = re.compile(
            r"投注单|待结算|最受欢迎|我的联赛|所有赛事|滚球盘|输赢盘|串关|登录|公告|偏好|"
            r"网址|连接到|耐心等待|禁用|电子竞技|亚洲界面|感谢您|可添加最爱|上限|"
            r"球队总得分|^过关$|^早盘$|"
            r"^体育$|^足球$|^篮球$|^比赛$|^今天$|^网球$|^排球$|"
            r"^(?:[12]H|HT|FT|OT|ET|Q[1-4]|PEN|AH|H1|H2)$"
        )
    n = (name or "").strip().replace("\u200e", "").replace("\u200f", "")
    if len(n) < 2 or len(n) > 40:
        return True
    if n.isdigit():
        return True
    return bool(_UI_JUNK_TEAM_RE.search(n))


def _team_similarity(a: str, b: str) -> float:
    """队名相似度 0~1（规范化后 SequenceMatcher + 包含/字符重叠）。"""
    from difflib import SequenceMatcher

    x = _norm_team(a)
    y = _norm_team(b)
    if not x or not y:
        return 0.0
    if x == y:
        return 1.0
    # 短名包含：法兰克福 ⊂ 法兰克福XX / 特拉布宗 ⊂ 特拉布宗体育
    if len(x) >= 2 and len(y) >= 2 and (x in y or y in x):
        shorter, longer = (x, y) if len(x) <= len(y) else (y, x)
        if len(shorter) >= 2 and len(shorter) / max(len(longer), 1) >= 0.45:
            return max(0.82, SequenceMatcher(None, x, y).ratio())
    ratio = SequenceMatcher(None, x, y).ratio()
    # 中文译名顺序颠倒：雅典aek vs aekaek雅典
    if len(x) >= 3 and len(y) >= 3:
        sx, sy = "".join(sorted(x)), "".join(sorted(y))
        bag = SequenceMatcher(None, sx, sy).ratio()
        ratio = max(ratio, bag * 0.95)
    # 中文译名前缀包含："斯托克松德" vs "斯托桑"（全译 vs 缩译）
    # 条件收紧防误匹配：共享前缀≥2字、前缀后双方还有剩余、长度差≥2
    # （"斯托克城/斯托克港"同为5字完整名不适用；"皇家马德里/皇家社会"长度差1不适用）
    if len(x) >= 2 and len(y) >= 2 and x != y:
        pfx = 0
        for a, b in zip(x, y):
            if a != b:
                break
            pfx += 1
        if (
            pfx >= 2
            and pfx < min(len(x), len(y))
            and abs(len(x) - len(y)) >= 2
        ):
            ratio = max(ratio, 0.75)
    # 中文译名差异（如"马尔默" vs "马模"）：用共有字符占比补齐
    if len(x) >= 2 and len(y) >= 2:
        cx, cy = set(x), set(y)
        common = cx & cy
        union = cx | cy
        if union:
            jaccard = len(common) / len(union)
            # Jaccard 高时直接采用
            if jaccard >= 0.6:
                ratio = max(ratio, jaccard * 0.92)
            # 3字以上中文名用 overlap 系数：共有字符 / 较短名长度
            # "马尔默"(3字) vs "马模"(2字)：overlap = 1/2 = 0.5
            # 不适用于2字名（"曼联" vs "曼城" 仅差1字会误匹配）
            elif len(x) >= 3 and len(y) >= 2:
                overlap = len(common) / min(len(cx), len(cy))
                if overlap >= 0.5:
                    ratio = max(ratio, overlap * 0.85)
        # 中文译名首字相同（同音字 transliteration 起始一致）+ 长度差 >= 1
        # "马尔默" vs "马模"：首字"马"相同，长度 3 vs 2
        if len(x) >= 3 and len(y) >= 2 and x[0] == y[0] and abs(len(x) - len(y)) >= 1:
            # 首字相同且 overlap >= 0.4，给译名差异额外加分
            if not common:
                common = {x[0]}
            ov = len(common) / min(len(cx), len(cy))
            if ov >= 0.4:
                ratio = max(ratio, 0.55)
    return ratio


def _pair_similarity(home_a: str, away_a: str, home_b: str, away_b: str) -> float:
    """同场两边队名相似度；允许主客对调。

    要求两队各自相似度 >= 0.55，防止单队完美匹配但另一队完全不同时误判。
    """
    sa1 = _team_similarity(home_a, home_b)
    sa2 = _team_similarity(away_a, away_b)
    direct = (sa1 + sa2) / 2.0

    sb1 = _team_similarity(home_a, away_b)
    sb2 = _team_similarity(away_a, home_b)
    swapped = (sb1 + sb2) / 2.0

    best = max(direct, swapped)
    # 单队相似度过低时拉低整体分数，防止"曼联vs曼城"因"利物浦"匹配而误判
    # 分档：<0.35 完全不同队 ×0.72；0.35~0.55 可能译名差异 ×0.90
    if best == direct:
        lo = min(sa1, sa2)
    else:
        lo = min(sb1, sb2)
    if lo < 0.35:
        best = best * 0.72
    elif lo < 0.55:
        best = best * 0.90
    return best


def _recover_teams_from_league(home: str, away: str, league: str) -> tuple[str, str]:
    """
    平博 DOM 常把 1H/HT 当成主队，真实对阵落在 league 字段「主 客」。
    若主/客是节次垃圾名，尝试从 league 恢复。
    """
    import re

    if not (_is_junk_team_name(home) or _is_junk_team_name(away)):
        return home, away
    raw = (league or "").replace("\u200e", " ").replace("\u200f", " ")
    parts = [p for p in re.split(r"\s+", raw.strip()) if p and not _is_junk_team_name(p)]
    # 取较长的两个队名候选
    parts = sorted(parts, key=len, reverse=True)
    if len(parts) >= 2:
        # 保持原文大致顺序：按在 league 中出现位置排序
        ordered = sorted(parts[:4], key=lambda p: raw.find(p))
        if len(ordered) >= 2:
            return ordered[0][:100], ordered[1][:100]
    return home, away

def _naive_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_start(value: str) -> datetime:
    # 无开赛时间时用「远过去」占位（列非空）；禁止用 now，否则会被当成已开赛
    if not value:
        return datetime(2000, 1, 1)
    try:
        raw = str(value).strip()
        # 毫秒/秒时间戳
        if raw.isdigit() and len(raw) >= 10:
            ts = int(raw)
            if ts > 10_000_000_000:
                ts = ts / 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)
        # ISO 或常见格式
        cleaned = raw.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(cleaned)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
                try:
                    dt = datetime.strptime(raw[:19], fmt)
                    break
                except ValueError:
                    continue
            else:
                return datetime.now(timezone.utc).replace(tzinfo=None)
        if dt.tzinfo:
            return dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except Exception:
        return datetime.now(timezone.utc).replace(tzinfo=None)


def _sport_from_str(value: str) -> SportType | None:
    """将字符串映射为 SportType；仅支持足球/篮球，其余返回 None。"""
    from app.services.bookmakers.sport_classify import normalize_sport

    n = normalize_sport(value)
    if n == "football":
        return SportType.FOOTBALL
    if n == "basketball":
        return SportType.BASKETBALL
    return None

async def _apply_score_clock(match: Match, rm) -> None:
    """把远程比分 / 进行时间写回本地赛事。"""
    match.home_score = int(getattr(rm, "home_score", 0) or 0)
    match.away_score = int(getattr(rm, "away_score", 0) or 0)
    extra = dict(match.extra_data or {})
    extra["source"] = extra.get("source") or "ob_live"
    clock = str(getattr(rm, "clock", "") or "").strip()
    period = str(getattr(rm, "period", "") or "").strip()
    if clock:
        extra["clock"] = clock
    elif rm.status != "live":
        extra.pop("clock", None)
    if period:
        extra["period"] = period
    elif rm.status != "live":
        extra.pop("period", None)
    match.extra_data = extra
    # 完场降级，避免「进行中」残留旧赛
    rm_status_l = str(getattr(rm, "status", "") or "").lower()
    if period == "完场" or rm_status_l == "finished":
        match.status = MatchStatus.FINISHED
        if getattr(match, "end_time", None) is None:
            match.end_time = datetime.now(timezone.utc)
        extra.pop("clock", None)
        extra.pop("period", None)
        match.extra_data = extra
    elif rm_status_l in ("cancelled", "canceled", "postponed"):
        # 取消/延期：置 CANCELLED，结算侧退本金（不按比分判输赢）
        match.status = MatchStatus.CANCELLED
        if getattr(match, "end_time", None) is None:
            match.end_time = datetime.now(timezone.utc)
        extra.pop("clock", None)
        extra.pop("period", None)
        match.extra_data = extra
    elif rm_status_l == "live":
        match.end_time = None


async def _resolve_match_id(db: AsyncSession, rm, local_by_id: dict[int, Match]) -> int | None:
    """将 RemoteMatch 映射到本地 Match.id；跨站同场合并到同一 Match，多 provider 共存。"""
    ext = rm.external_id or ""
    prefix = ext.split(":", 1)[0].lower() if ":" in ext else ""
    from app.services.bookmakers.plugins.ob.odds import is_virtual_match

    # 严格拦截 EAFC / 虚拟盘，禁止入库与更新
    if is_virtual_match(
        getattr(rm, "sport", "") or "",
        getattr(rm, "league", "") or "",
        getattr(rm, "home_team", "") or "",
        getattr(rm, "away_team", "") or "",
    ):
        return None

    # 平博 DOM 常把 1H/HT 当主队：先从 league 恢复真实对阵
    home_fix, away_fix = _recover_teams_from_league(
        getattr(rm, "home_team", "") or "",
        getattr(rm, "away_team", "") or "",
        getattr(rm, "league", "") or "",
    )
    rm.home_team = home_fix
    rm.away_team = away_fix

    if _is_junk_team_name(home_fix) or _is_junk_team_name(away_fix):
        return None

    # 禁止 demo:/短数字 id 映射到本地场；仅接受真实站长 external_id
    if ":" in ext:
        pfx, suffix = ext.split(":", 1)
        if pfx == "demo" or (suffix.isdigit() and len(suffix) <= 4):
            return None

    # 按 external_id 查找
    if ext:
        with db.no_autoflush:
            res = await db.execute(
                select(Match).options(selectinload(Match.odds)).where(Match.external_id == ext).limit(1)
            )
            found = res.scalar_one_or_none()
        if found:
            sport = _sport_from_str(rm.sport)
            if sport is None:
                return None
            # 球类冲突：不覆盖已有正确球类；矛盾则拒绝更新
            from app.services.bookmakers.sport_classify import reject_sport_mismatch, sports_compatible

            existing = found.sport.value if hasattr(found.sport, "value") else str(found.sport)
            if not sports_compatible(existing, sport.value):
                logger.warning(
                    "sport conflict skip upsert ext=%s existing=%s remote=%s",
                    ext,
                    existing,
                    sport.value,
                )
                return None
            if reject_sport_mismatch(
                sport.value,
                period=str(getattr(rm, "period", "") or ""),
                home_score=getattr(rm, "home_score", 0) or 0,
                away_score=getattr(rm, "away_score", 0) or 0,
                text=f"{rm.league} {rm.home_team} {rm.away_team}",
            ):
                return None
            # 已有球类保持不变（避免错误远端覆盖）
            new_lg = (rm.league or "").strip()
            old_lg = (found.league or "").strip()
            _ph = {"足球滚球", "篮球滚球", "滚球", "体育", "足球", "篮球", "", "未知联赛"}
            if new_lg and new_lg not in _ph:
                found.league = new_lg[:100]
            elif not old_lg or old_lg in _ph:
                found.league = (new_lg or old_lg or "体育")[:100]
            # 队名：远端非垃圾名才覆盖（防 1H/HT 写回）
            if not _is_junk_team_name(rm.home_team or ""):
                found.home_team = rm.home_team[:100]
            if not _is_junk_team_name(rm.away_team or ""):
                found.away_team = rm.away_team[:100]
            found.start_time = _parse_start(rm.start_time)
            # 同步路径只写滚球；非 live 远端不降级已有 LIVE（由 demote 处理）
            if str(getattr(rm, "status", "") or "").lower() == "live":
                found.status = MatchStatus.LIVE
            found.venue = (rm.venue or found.venue or "")[:200]
            await _apply_score_clock(found, rm)
            extra = dict(found.extra_data or {})
            ids = dict(extra.get("ids") or {})
            if prefix:
                ids[prefix] = ext
            extra["ids"] = ids
            extra["source"] = f"{prefix or 'site'}_live"
            found.extra_data = extra
            local_by_id[found.id] = found
            return found.id

    # 同站模糊匹配复用；禁止跨站合并（OB / 平博各自独立分类）
    sport = _sport_from_str(rm.sport)
    if sport is None:
        return None
    # 修复平博 DOM 把 1H/HT 当队名
    home_fix, away_fix = _recover_teams_from_league(
        getattr(rm, "home_team", "") or "",
        getattr(rm, "away_team", "") or "",
        getattr(rm, "league", "") or "",
    )
    if _is_junk_team_name(home_fix) or _is_junk_team_name(away_fix):
        return None
    rm.home_team = home_fix
    rm.away_team = away_fix

    rm_start = _parse_start(rm.start_time)
    best_id: int | None = None
    best_score = 0.0
    site_prefix = (prefix or "").lower()
    for m in list(local_by_id.values()):
        if _is_junk_team_name(m.home_team) or _is_junk_team_name(m.away_team):
            continue
        m_ext = str(m.external_id or "")
        m_prefix = m_ext.split(":", 1)[0].lower() if ":" in m_ext else ""
        if site_prefix and m_prefix and m_prefix != site_prefix:
            continue
        if site_prefix and not m_prefix:
            # 无前缀旧数据：看 extra_data.source
            src = str((m.extra_data or {}).get("source") or "")
            if src and not src.startswith(f"{site_prefix}_"):
                continue
        m_sport = (m.sport.value if hasattr(m.sport, "value") else str(m.sport)).lower()
        if m_sport in ("soccer",):
            m_sport = "football"
        if m_sport != sport.value:
            continue
        try:
            if m.start_time and rm_start:
                delta = abs((m.start_time - rm_start).total_seconds())
            else:
                delta = 0
        except Exception:
            delta = 0
        if delta > 6 * 3600:
            continue
        score = _pair_similarity(m.home_team, m.away_team, home_fix, away_fix)
        if score < 0.72:
            continue
        if score > best_score:
            best_score = score
            best_id = m.id
    if best_id is not None:
        m = local_by_id[best_id]
        extra = dict(m.extra_data or {})
        ids = dict(extra.get("ids") or {})
        if prefix and ext:
            ids[prefix] = ext
            if (not m.external_id) or (
                str(m.external_id).startswith(f"{prefix}:")
                and len(str(m.external_id).split(":", 1)[-1]) <= 4
            ):
                m.external_id = ext
        extra["ids"] = ids
        extra["source"] = f"{prefix or 'site'}_live"
        extra["site_code"] = prefix or site_prefix
        extra["merge_score"] = round(best_score, 3)
        m.extra_data = extra
        if len(_norm_team(home_fix)) > len(_norm_team(m.home_team)):
            m.home_team = home_fix[:100]
        if len(_norm_team(away_fix)) > len(_norm_team(m.away_team)):
            m.away_team = away_fix[:100]
        # 联赛：真实名优先于「足球滚球/篮球滚球」占位
        new_lg = (rm.league or "").strip()
        old_lg = (m.league or "").strip()
        placeholders = {"足球滚球", "篮球滚球", "滚球", "体育", "足球", "篮球", "", "未知联赛"}
        if new_lg and new_lg not in placeholders:
            m.league = new_lg[:100]
        elif not old_lg or old_lg in placeholders:
            m.league = (new_lg or old_lg or "体育")[:100]
        m.venue = (rm.venue or m.venue or "")[:200]
        if rm.status == "live":
            m.status = MatchStatus.LIVE
        await _apply_score_clock(m, rm)
        return m.id
    # 真实赛事：创建本地记录（只允许滚球 LIVE）；每站独立一行
    if not home_fix or not away_fix:
        return None
    if str(getattr(rm, "status", "") or "").lower() != "live":
        return None
    from app.services.bookmakers.match_live import remote_match_started

    if not remote_match_started(rm):
        return None
    match = Match(
        external_id=ext or None,
        sport=sport,
        league=(rm.league or "体育")[:100],
        home_team=home_fix[:100],
        away_team=away_fix[:100],
        start_time=rm_start,
        status=MatchStatus.LIVE,
        venue=(rm.venue or "")[:200],
        home_score=int(getattr(rm, "home_score", 0) or 0),
        away_score=int(getattr(rm, "away_score", 0) or 0),
        extra_data={
            "source": f"{prefix or 'site'}_live",
            "site_code": prefix or "",
            "ids": {prefix: ext} if prefix and ext else {},
            "clock": str(getattr(rm, "clock", "") or ""),
            "period": str(getattr(rm, "period", "") or ""),
        },
    )
    db.add(match)
    await db.flush()
    from sqlalchemy.orm.attributes import set_committed_value
    set_committed_value(match, "odds", [])
    local_by_id[match.id] = match
    return match.id
