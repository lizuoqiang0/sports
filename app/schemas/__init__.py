"""
Pydantic Schemas - 请求/响应模型
"""
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, EmailStr, Field, ConfigDict, model_validator
from app.config import settings


# === 通用 ===
class APIResponse(BaseModel):
    """统一响应格式"""
    success: bool = True
    message: str = "ok"
    data: Optional[Any] = None
    error_code: Optional[str] = None


# === 鉴权 ===
class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=50, description="用户名")
    email: EmailStr = Field(..., description="邮箱")
    password: str = Field(..., min_length=8, max_length=128, description="密码(最少8位)")

    model_config = ConfigDict(use_enum_values=True)


class LoginRequest(BaseModel):
    username: str = Field(..., description="用户名或邮箱")
    password: str = Field(..., description="密码")


class RefreshTokenRequest(BaseModel):
    refresh_token: str


class UserInfoResponse(BaseModel):
    id: int
    username: str
    email: str
    role: str
    balance: Decimal
    ai_enabled: bool
    is_active: bool
    is_verified: bool
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# === 赛事 ===
class MatchListRequest(BaseModel):
    sport: Optional[str] = None
    status: Optional[str] = "upcoming"
    league: Optional[str] = None
    page: int = Field(1, ge=1)
    page_size: int = Field(20, ge=1, le=100)


class MatchResponse(BaseModel):
    id: int
    sport: str
    league: str
    home_team: str
    away_team: str
    start_time: datetime
    end_time: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    status: str
    home_score: int
    away_score: int
    venue: Optional[str] = None
    clock: Optional[str] = None
    period: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)

    @model_validator(mode="wrap")
    @classmethod
    def pull_clock_period(cls, data, handler):
        obj = handler(data)
        extra = {}
        if hasattr(data, "extra_data"):
            extra = data.extra_data or {}
        elif isinstance(data, dict):
            extra = data.get("extra_data") or {}
        if not obj.clock:
            obj.clock = (extra.get("clock") or "") or None
        if not obj.period:
            obj.period = (extra.get("period") or "") or None
        if not obj.finished_at and str(getattr(obj, "status", "") or "").lower() == "finished":
            obj.finished_at = obj.end_time or obj.updated_at or obj.start_time
        return obj


class MatchDetailResponse(MatchResponse):
    odds: List["OddsResponse"] = []
    extra_data: dict = {}

    model_config = ConfigDict(from_attributes=True)


# === 赔率 ===
class OddsResponse(BaseModel):
    id: int
    match_id: int
    bet_type: str
    odds_data: dict
    spread: Optional[float] = None
    total: Optional[float] = None
    provider: str
    is_live: bool
    valid_from: datetime

    model_config = ConfigDict(from_attributes=True)


class OddsSubscriptionRequest(BaseModel):
    match_ids: List[int] = Field(..., description="要订阅的赛事ID列表")
    bet_types: Optional[List[str]] = None


# === 投注 ===
class PlaceBetRequest(BaseModel):
    match_id: int = Field(..., description="赛事ID")
    bet_type: str = Field(..., description="投注类型: total（全场大小球）")
    selection: str = Field(..., description="选择: under / over")
    stake: Decimal = Field(..., gt=0, description="投注金额")
    odds: float = Field(..., gt=1.0, description="确认赔率(防篡改)")
    # 可选：指定站点 code（ob/pinnacle）或站点中文名
    provider: Optional[str] = Field(None, description="投注站点: ob/pinnacle")

    model_config = ConfigDict(use_enum_values=True)


class PlaceBetResponse(BaseModel):
    bet_id: int
    status: str
    stake: Decimal
    odds: float
    potential_payout: Decimal
    balance_after: Decimal


# === AI投注 ===
class AIConfigRequest(BaseModel):
    is_active: bool = False
    max_bet_amount: Decimal = Field(settings.AI_STRATEGY_MAX_BET_AMOUNT, gt=0)
    max_daily_bets: int = Field(settings.AI_STRATEGY_MAX_DAILY_BETS, ge=1, le=100)
    min_confidence: float = Field(settings.AI_MIN_CONFIDENCE, ge=0.0, le=0.99)
    preferred_sports: List[str] = []
    excluded_teams: List[str] = []
    stop_loss: Decimal = Field(settings.AI_STOP_LOSS, gt=0)
    take_profit: Decimal = Field(settings.AI_TAKE_PROFIT, gt=0)
    max_odds: float = Field(settings.AI_MAX_ODDS, gt=1.0)
    min_odds: float = Field(settings.AI_MIN_ODDS, gt=1.0)
    use_llm_analysis: bool = True


class AIRecommendationResponse(BaseModel):
    match_id: int
    recommendation: str  # home/away/draw
    confidence: float
    reasoning: str
    suggested_stake: Decimal


class AIBetExecutionRequest(BaseModel):
    enabled: bool


# === WebSocket消息 ===
class WSMessage(BaseModel):
    type: str
    channel: Optional[str] = None
    data: Optional[dict] = None
    timestamp: datetime = Field(default_factory=datetime.now)


# === 博彩站点 ===
class BookmakerAccountUpdate(BaseModel):
    code: str
    base_url: str = ""
    username: str = ""
    password: Optional[str] = None  # 空则不更新密码
    session_token: Optional[str] = None  # 空则不更新；粘贴的 X-API-TOKEN
    enabled: bool = True


class BookmakerBatchUpdateRequest(BaseModel):
    accounts: List[BookmakerAccountUpdate]


class BookmakerVerifyBatchRequest(BaseModel):
    """并行验证多个站点；codes 为空则验证已填网址的站点。"""
    codes: Optional[List[str]] = None
    manual_venue: bool = False
