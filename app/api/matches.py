"""
赛事 API - 赛事列表 / 详情 / 搜索
"""
import logging
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, or_
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.user import Match, MatchStatus, SportType, Odds
from app.core.security import get_current_user
from app.core.cache import cache
from app.schemas import APIResponse, MatchResponse, MatchDetailResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/matches", tags=["赛事"])

# 仅支持的运动类型
_SUPPORTED_SPORTS = [SportType.FOOTBALL, SportType.BASKETBALL]

_CLOCK_RE = re.compile(r"^(\d{1,3}):(\d{2})$")


def _parse_clock_seconds(clock: str) -> Optional[int]:
    """解析 MM:SS / M:SS → 秒；失败返回 None。"""
    if not clock:
        return None
    m = _CLOCK_RE.match(str(clock).strip())
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def _match_elapsed_seconds(m: Match) -> int:
    """比赛已进行时长（秒），用于列表排序；无时钟则排最后。"""
    extra = m.extra_data if isinstance(m.extra_data, dict) else {}
    clock = str(getattr(m, "clock", None) or extra.get("clock") or "")
    period = str(getattr(m, "period", None) or extra.get("period") or "")
    secs = _parse_clock_seconds(clock)
    if secs is None:
        return 10**9  # 无时长靠后
    # 中场若时钟异常偏小，按 45:00 计
    if "中场" in period and secs < 40 * 60:
        return 45 * 60
    if "加时上" in period:
        return 90 * 60 + secs
    if "加时下" in period:
        return 105 * 60 + secs
    if "加时" in period:
        return 90 * 60 + secs
    # OB 足球时钟多为累计分钟（上半场/下半场同一套），直接用
    return secs


def _not_virtual_filters():
    """API 层兜底：排除足球/篮球虚拟盘与未知联赛。"""
    return and_(
        Match.league.is_not(None),
        Match.league != "",
        Match.league != "未知联赛",
        Match.league != "未知",
        ~Match.league.in_(["N/A", "-", "—"]),
        ~Match.league.ilike("%EAFC%"),
        ~Match.league.ilike("%EA FC%"),
        ~Match.league.ilike("%FIFA%"),
        ~Match.league.ilike("%电子足球%"),
        ~Match.league.ilike("%电子篮球%"),
        ~Match.league.ilike("%虚拟%"),
        ~Match.league.ilike("%eFootball%"),
        ~Match.league.ilike("%PANDA%"),
        ~Match.league.ilike("VS-%"),
        ~Match.league.ilike("VS %"),
        ~Match.league.ilike("%NBA 2K%"),
        ~Match.league.ilike("%NBA2K%"),
        ~Match.league.ilike("%2K2%"),
        ~Match.league.ilike("%FC24%"),
        ~Match.league.ilike("%FC25%"),
        ~Match.league.ilike("%FC26%"),
        ~Match.league.ilike("%瓦尔基里%"),
        ~Match.league.ilike("%瓦尔哈拉%"),
        ~Match.league.ilike("%Valkyrie%"),
        ~Match.league.ilike("%Valhalla%"),
        ~Match.league.ilike("%手柄%"),
        ~Match.league.op("~*")(r"\(\s*\d{1,2}\s*分钟\s*\)"),
        ~Match.home_team.ilike("%EAFC%"),
        ~Match.away_team.ilike("%EAFC%"),
        ~Match.home_team.ilike("%PANDA%"),
        ~Match.away_team.ilike("%PANDA%"),
        # 中国赛事（含港澳台国内联赛）
        ~Match.league.ilike("%中超%"),
        ~Match.league.ilike("%中甲%"),
        ~Match.league.ilike("%中乙%"),
        ~Match.league.ilike("%女超%"),
        ~Match.league.ilike("%足协杯%"),
        ~Match.league.ilike("%中国%"),
        ~Match.league.ilike("%China%"),
        ~Match.league.ilike("%CSL%"),
        ~Match.league.ilike("%CBA%"),
        ~Match.league.ilike("%WCBA%"),
        ~Match.league.ilike("%港超%"),
        ~Match.league.ilike("%香港%"),
        ~Match.league.ilike("%台湾%"),
        ~Match.league.ilike("%台灣%"),
        ~Match.league.ilike("%澳门%"),
        ~Match.league.ilike("%澳門%"),
    )


def _site_code_of(m: Match) -> str:
    from app.services.provider_utils import site_code_from_match

    return site_code_from_match(m) or ""


def _serialize_match_list_item(m: Match, *, provider_code: str = "") -> dict:
    """列表项：基础字段 + 比分时钟 + 当前有效赔率。"""
    item = MatchResponse.model_validate(m).model_dump(mode="json")
    extra = m.extra_data or {}
    item["clock"] = item.get("clock") or extra.get("clock") or ""
    period = item.get("period") or extra.get("period") or ""
    # 兼容历史脏数据：阶段14 → 第2节
    if isinstance(period, str) and period.startswith("阶段") and period[2:].isdigit():
        from app.services.bookmakers.plugins.ob.odds import _PERIOD_LABELS

        period = _PERIOD_LABELS.get(period[2:], period)
    item["period"] = period
    site = _site_code_of(m)
    item["site_code"] = site
    item["site_name"] = {"ob": "OB体育", "pinnacle": "平博"}.get(site, site or "")
    provider_names = {
        "ob": ("OB体育", "OB Sports", "OB"),
        "pinnacle": ("平博", "Pinnacle"),
    }
    allow_providers = provider_names.get(provider_code) if provider_code else None
    odds_out = []
    for o in (m.odds or []):
        if o.valid_to is not None:
            continue
        if allow_providers and str(o.provider or "") not in allow_providers:
            continue
        odds_out.append({
            "bet_type": o.bet_type.value if hasattr(o.bet_type, "value") else str(o.bet_type),
            "odds_data": o.odds_data,
            "spread": o.spread,
            "total": o.total,
            "provider": o.provider,
            "is_live": o.is_live,
        })
    item["odds"] = odds_out
    return item


# === 赛事列表 ===
@router.get("", response_model=APIResponse)
async def list_matches(
    sport: Optional[str] = Query(None, description="运动类型"),
    status_filter: Optional[str] = Query(None, alias="status", description="赛事状态"),
    league: Optional[str] = Query(None, description="联赛名称"),
    provider: Optional[str] = Query(None, description="站点分类：ob / pinnacle"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=300),
    db: AsyncSession = Depends(get_db),
    _: object = Depends(get_current_user),
):
    """获取赛事列表；可按站点分类（OB / 平博）筛选。"""
    provider_code = (provider or "").strip().lower()
    if provider_code and provider_code not in ("ob", "pinnacle"):
        provider_code = ""

    cache_key = (
        f"matches:list:v4nochina:{sport}:{status_filter}:{league}:{provider_code}:{page}:{page_size}"
    )
    cached = await cache.get_json(cache_key)
    if cached:
        return APIResponse(data=cached)

    query = select(Match).options(selectinload(Match.odds))
    count_query = select(Match.id)

    filters = [Match.sport.in_(_SUPPORTED_SPORTS), _not_virtual_filters()]
    if sport:
        filters.append(Match.sport == sport)
    if status_filter:
        filters.append(Match.status == status_filter)
    if league:
        filters.append(Match.league.ilike(f"%{league}%"))
    if filters:
        query = query.where(and_(*filters))
        count_query = count_query.where(and_(*filters))

    total_result = await db.execute(count_query)
    total = len(total_result.scalars().all())

    result = await db.execute(query)
    all_matches = list(result.scalars().all())

    from app.services.bookmakers.china_match import is_china_match

    all_matches = [
        m
        for m in all_matches
        if not is_china_match(
            m.league or "",
            m.home_team or "",
            m.away_team or "",
            m.sport.value if hasattr(m.sport, "value") else str(m.sport),
        )
    ]

    # JSON 过滤在部分库上可能漏网：再按 site_code 兜底
    if provider_code:
        all_matches = [m for m in all_matches if _site_code_of(m) == provider_code]

    if status_filter == "live":
        from app.services.bookmakers.match_live import local_match_started

        all_matches = [m for m in all_matches if local_match_started(m)]
        all_matches.sort(key=lambda m: (_match_elapsed_seconds(m), -(m.updated_at.timestamp() if m.updated_at else 0)))
        total = len(all_matches)
    elif status_filter == "finished":
        all_matches.sort(
            key=lambda m: (
                _match_elapsed_seconds(m) if _match_elapsed_seconds(m) < 10**9 else 0,
                -(m.start_time.timestamp() if m.start_time else 0),
            ),
            reverse=True,
        )
        total = len(all_matches)
    else:
        all_matches.sort(
            key=lambda m: (
                _match_elapsed_seconds(m) if _match_elapsed_seconds(m) < 10**9 else 10**9,
                m.start_time.timestamp() if m.start_time else 0,
            )
        )
        total = len(all_matches)

    offset = (page - 1) * page_size
    matches = all_matches[offset : offset + page_size]

    data = {
        "items": [_serialize_match_list_item(m, provider_code=provider_code) for m in matches],
        "total": total,
        "page": page,
        "page_size": page_size,
        "has_more": offset + len(matches) < total,
        "provider": provider_code or None,
    }

    await cache.set_json(cache_key, data, ttl=3 if status_filter == "live" else 10)

    return APIResponse(data=data)


# === 按运动类型分组（须在 /{match_id} 之前注册）===
@router.get("/sports/grouped", response_model=APIResponse)
async def get_matches_grouped(
    status_filter: str = Query("upcoming", description="赛事状态"),
    db: AsyncSession = Depends(get_db),
    _: object = Depends(get_current_user),
):
    """按运动类型分组获取赛事"""
    query = select(Match).where(
        Match.status == status_filter,
        Match.sport.in_(_SUPPORTED_SPORTS),
        _not_virtual_filters(),
    ).order_by(Match.start_time.asc())
    result = await db.execute(query)
    matches = result.scalars().all()
    if str(status_filter).lower() == "live":
        from app.services.bookmakers.match_live import local_match_started

        matches = [m for m in matches if local_match_started(m)]

    grouped: dict = {}
    for m in matches:
        sport = m.sport.value if hasattr(m.sport, "value") else str(m.sport)
        if sport not in grouped:
            grouped[sport] = []
        grouped[sport].append({
            "id": m.id,
            "league": m.league,
            "home_team": m.home_team,
            "away_team": m.away_team,
            "start_time": m.start_time.isoformat() if m.start_time else None,
            "status": m.status.value if hasattr(m.status, "value") else m.status,
            "home_score": m.home_score,
            "away_score": m.away_score,
        })

    return APIResponse(data=grouped)


# === 搜索赛事 ===
@router.get("/search", response_model=APIResponse)
async def search_matches(
    q: str = Query(..., min_length=1, description="搜索关键词(球队/联赛)"),
    limit: int = Query(10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
    _: object = Depends(get_current_user),
):
    """搜索赛事"""
    search_term = f"%{q}%"
    query = select(Match).where(
        Match.sport.in_(_SUPPORTED_SPORTS),
        _not_virtual_filters(),
        or_(
            Match.home_team.ilike(search_term),
            Match.away_team.ilike(search_term),
            Match.league.ilike(search_term),
        ),
    ).order_by(Match.start_time.asc()).limit(limit)

    result = await db.execute(query)
    matches = result.scalars().all()

    return APIResponse(data=[
        {
            "id": m.id,
            "sport": m.sport.value if hasattr(m.sport, "value") else str(m.sport),
            "league": m.league,
            "home_team": m.home_team,
            "away_team": m.away_team,
            "start_time": m.start_time.isoformat() if m.start_time else None,
            "status": m.status.value if hasattr(m.status, "value") else m.status,
            "site_code": (_site_code_of(m) or ""),
            "site_name": {"ob": "OB体育", "pinnacle": "平博"}.get(_site_code_of(m), ""),
        }
        for m in matches
    ])


# === 直播中赛事 ===
@router.get("/live/now", response_model=APIResponse)
async def get_live_matches(
    db: AsyncSession = Depends(get_db),
    _: object = Depends(get_current_user),
):
    """获取正在进行中的赛事"""
    result = await db.execute(
        select(Match)
        .options(selectinload(Match.odds))
        .where(
            Match.status == MatchStatus.LIVE,
            Match.sport.in_(_SUPPORTED_SPORTS),
            _not_virtual_filters(),
        )
    )
    matches = list(result.scalars().all())
    from app.services.bookmakers.match_live import local_match_started

    matches = [m for m in matches if local_match_started(m)]
    matches.sort(key=lambda m: (_match_elapsed_seconds(m), -(m.updated_at.timestamp() if m.updated_at else 0)))

    return APIResponse(data=[
        {
            "id": m.id,
            "sport": m.sport.value if hasattr(m.sport, "value") else str(m.sport),
            "league": m.league,
            "home_team": m.home_team,
            "away_team": m.away_team,
            "home_score": m.home_score,
            "away_score": m.away_score,
            "odds": [{
                "bet_type": o.bet_type.value if hasattr(o.bet_type, "value") else str(o.bet_type),
                "odds_data": o.odds_data,
                "is_live": o.is_live,
            } for o in m.odds if o.is_live and o.valid_to is None]
        }
        for m in matches
    ])


# === 联赛列表 ===
@router.get("/leagues", response_model=APIResponse)
async def list_leagues(
    db: AsyncSession = Depends(get_db),
    _: object = Depends(get_current_user),
):
    """获取联赛名称列表"""
    result = await db.execute(
        select(Match.league).where(
            Match.status.in_([MatchStatus.UPCOMING, MatchStatus.LIVE]),
            Match.sport.in_(_SUPPORTED_SPORTS),
            _not_virtual_filters(),
        ).distinct()
    )
    leagues = [row[0] for row in result.all() if row[0]]
    return APIResponse(data={"items": sorted(leagues), "total": len(leagues)})


# === 赛事详情（动态路径放最后）===
@router.get("/{match_id}", response_model=APIResponse)
async def get_match(
    match_id: int,
    db: AsyncSession = Depends(get_db),
    _: object = Depends(get_current_user),
):
    """获取赛事详情 (含赔率)"""
    result = await db.execute(
        select(Match)
        .options(selectinload(Match.odds))
        .where(Match.id == match_id)
    )
    match = result.scalar_one_or_none()

    if not match:
        raise HTTPException(status_code=404, detail="赛事不存在")
    if match.sport not in _SUPPORTED_SPORTS:
        raise HTTPException(status_code=404, detail="赛事不存在")
    from app.services.bookmakers.china_match import is_china_match
    from app.services.bookmakers.plugins.ob.odds import is_virtual_match

    sport_v = match.sport.value if hasattr(match.sport, "value") else str(match.sport)
    if is_virtual_match(sport_v, match.league or "", match.home_team or "", match.away_team or ""):
        raise HTTPException(status_code=404, detail="赛事不存在")
    if is_china_match(match.league or "", match.home_team or "", match.away_team or "", sport_v):
        raise HTTPException(status_code=404, detail="赛事不存在")

    # 构建详情响应
    detail = MatchDetailResponse.model_validate(match)
    odds_list = [o for o in match.odds if o.valid_to is None]  # 只取当前有效赔率

    data = detail.model_dump(mode="json")
    extra = match.extra_data or {}
    data["clock"] = data.get("clock") or extra.get("clock") or ""
    data["period"] = data.get("period") or extra.get("period") or ""
    data["odds"] = [{
        "id": o.id,
        "bet_type": o.bet_type.value if hasattr(o.bet_type, "value") else o.bet_type,
        "odds_data": o.odds_data,
        "spread": o.spread,
        "total": o.total,
        "provider": o.provider,
        "is_live": o.is_live,
        "valid_from": o.valid_from.isoformat() if o.valid_from else None,
    } for o in odds_list]

    return APIResponse(data=data)