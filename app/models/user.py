"""
ORM 数据模型
"""
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Optional

from sqlalchemy import (
    BigInteger, String, Boolean, DateTime, Numeric,
    Integer, ForeignKey, Text, Index, JSON, UniqueConstraint, text
)
from sqlalchemy.orm import relationship, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from app.database import Base


# === 自定义类型 ===
class DateTimeUTC(TypeDecorator):
    """确保时区一致的DateTime类型"""
    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None and value.tzinfo is not None:
            # asyncpg TIMESTAMP WITHOUT TIME ZONE 需要 naive datetime
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value


# === 枚举 ===
class UserRole(str, Enum):
    ADMIN = "admin"
    USER = "user"
    VIP = "vip"
    BOT = "bot"  # AI投注机器人账户


class MatchStatus(str, Enum):
    UPCOMING = "upcoming"       # 未开始
    LIVE = "live"              # 进行中
    FINISHED = "finished"      # 已结束
    CANCELLED = "cancelled"    # 已取消
    POSTPONED = "postponed"    # 延期


class SportType(str, Enum):
    # 业务仅启用 football / basketball；其余保留以兼容 PG 枚举历史值
    FOOTBALL = "football"
    BASKETBALL = "basketball"
    TENNIS = "tennis"
    BASEBALL = "baseball"
    CRICKET = "cricket"
    HORSERACING = "horse_racing"
    MMA = "mma"
    VOLLEYBALL = "volleyball"
    TABLE_TENNIS = "table_tennis"
    BADMINTON = "badminton"
    SNOOKER = "snooker"
    HOCKEY = "hockey"
    AMERICAN_FOOTBALL = "american_football"
    OTHER = "other"

    @classmethod
    def _missing_(cls, value):
        """兼容数据库枚举名和外部接口的大小写差异。"""
        normalized = str(value or "").strip().lower()
        if normalized == "soccer":
            normalized = cls.FOOTBALL.value
        return next((member for member in cls if member.value == normalized), None)


class BetType(str, Enum):
    MONEYLINE = "moneyline"        # 胜负
    SPREAD = "spread"              # 让分
    TOTAL = "total"                # 全场大小球总分
    FIRST_HALF_TOTAL = "first_half_total"  # 上半场大小球（历史兼容）
    SECOND_HALF_TOTAL = "second_half_total"  # 下半场大小球（历史兼容）
    PROPOSITION = "prop"           # 特殊投注
    PARLAY = "parlay"              # 串关
    LIVE = "live"                  # 滚球


class BetStatus(str, Enum):
    SUCCESS = "success"     # 下单成功
    FAILED = "failed"       # 下单失败


class TransactionType(str, Enum):
    BET_PLACE = "bet_place"       # 下注
    AI_BET = "ai_bet"             # AI投注


class BookmakerStatus(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTED = "connected"
    ERROR = "error"


# === 用户模型 ===
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(default=UserRole.USER, nullable=False)

    # 钱包
    balance: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0, nullable=False)

    # AI设置
    ai_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    ai_risk_level: Mapped[str] = mapped_column(String(20), default="moderate")
    ai_daily_limit: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=1000)

    # 下单模式：manual=人工确认 / active=主动自动下单
    bet_mode: Mapped[str] = mapped_column(String(20), default="manual", nullable=False)

    # 状态
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)

    # 时间戳
    created_at: Mapped[datetime] = mapped_column(DateTimeUTC, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTimeUTC, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTimeUTC, nullable=True)

    # 关系
    bets: Mapped[list["Bet"]] = relationship("Bet", back_populates="user", cascade="all, delete-orphan")
    transactions: Mapped[list["Transaction"]] = relationship("Transaction", back_populates="user", cascade="all, delete-orphan")
    ai_config: Mapped[Optional["AIConfig"]] = relationship("AIConfig", back_populates="user", uselist=False, cascade="all, delete-orphan")
    bookmaker_accounts: Mapped[list["BookmakerAccount"]] = relationship(
        "BookmakerAccount", back_populates="user", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("idx_user_balance", "balance"),
    )


# === AI配置模型 ===
class AIConfig(Base):
    __tablename__ = "ai_configs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)

    # 策略配置
    strategy: Mapped[str] = mapped_column(
        String(50),
        default="simple",
        server_default=text("'simple'"),
        nullable=False,
    )  # only simple
    max_bet_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=100)
    max_daily_bets: Mapped[int] = mapped_column(Integer, default=10)
    min_confidence: Mapped[float] = mapped_column(default=0.0)
    preferred_sports: Mapped[list] = mapped_column(JSON, default=list)  # ["football", "basketball"]
    excluded_teams: Mapped[list] = mapped_column(JSON, default=list)

    # 风控
    stop_loss: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=500)  # 止损金额
    take_profit: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=1000)  # 止盈金额
    max_odds: Mapped[float] = mapped_column(default=10.0)  # 最高赔率
    min_odds: Mapped[float] = mapped_column(default=1.1)   # 最低赔率

    # 高级
    use_llm_analysis: Mapped[bool] = mapped_column(Boolean, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTimeUTC, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTimeUTC, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    user: Mapped[User] = relationship("User", back_populates="ai_config")


# === 赛事模型 ===
class Match(Base):
    __tablename__ = "matches"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    external_id: Mapped[Optional[str]] = mapped_column(String(100), index=True)  # 外部数据源ID

    sport: Mapped[SportType] = mapped_column(nullable=False, index=True)
    league: Mapped[str] = mapped_column(String(100), nullable=False)
    home_team: Mapped[str] = mapped_column(String(100), nullable=False)
    away_team: Mapped[str] = mapped_column(String(100), nullable=False)

    # 时间
    start_time: Mapped[datetime] = mapped_column(DateTimeUTC, nullable=False, index=True)
    end_time: Mapped[Optional[datetime]] = mapped_column(DateTimeUTC, nullable=True)

    # 状态
    status: Mapped[MatchStatus] = mapped_column(default=MatchStatus.UPCOMING, nullable=False, index=True)

    # 比分
    home_score: Mapped[int] = mapped_column(Integer, default=0)
    away_score: Mapped[int] = mapped_column(Integer, default=0)

    # 附加信息
    venue: Mapped[Optional[str]] = mapped_column(String(200))
    extra_data: Mapped[dict] = mapped_column(JSON, default=dict)

    # 时间戳
    created_at: Mapped[datetime] = mapped_column(DateTimeUTC, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTimeUTC, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    # 关系
    odds: Mapped[list["Odds"]] = relationship("Odds", back_populates="match", cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_match_sport_status", "sport", "status"),
        Index("idx_match_start", "start_time"),
    )


# === 赔率模型 ===
class Odds(Base):
    __tablename__ = "odds"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id", ondelete="CASCADE"), index=True)

    # 投注类型
    bet_type: Mapped[BetType] = mapped_column(nullable=False)

    # 赔率数据 (JSON存储不同选项的赔率)
    # 如: {"home": 1.85, "away": 2.10, "draw": 3.20}
    odds_data: Mapped[dict] = mapped_column(JSON, nullable=False)

    # 盘口信息（让分/大小球）
    spread: Mapped[Optional[float]] = mapped_column(default=0)  # 让分数
    total: Mapped[Optional[float]] = mapped_column(default=0)    # 大小球盘口线

    # 来源
    provider: Mapped[str] = mapped_column(String(50), default="OB Sports")
    is_live: Mapped[bool] = mapped_column(Boolean, default=False)

    # 时间戳
    valid_from: Mapped[datetime] = mapped_column(DateTimeUTC, default=lambda: datetime.now(timezone.utc))
    valid_to: Mapped[Optional[datetime]] = mapped_column(DateTimeUTC, nullable=True)
    # 最近一次从站点采集到该盘口；与“本版本何时开始”的 valid_from 分离。
    last_seen_at: Mapped[datetime] = mapped_column(DateTimeUTC, default=lambda: datetime.now(timezone.utc), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTimeUTC, default=lambda: datetime.now(timezone.utc))

    match: Mapped[Match] = relationship("Match", back_populates="odds")

    __table_args__ = (
        Index("idx_odds_match_type", "match_id", "bet_type"),
        UniqueConstraint("match_id", "bet_type", "provider", "valid_from", name="uq_odds_version"),
    )


# === 投注模型 ===
class Bet(Base):
    __tablename__ = "bets"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"), index=True)

    # 投注详情
    bet_type: Mapped[BetType] = mapped_column(nullable=False)
    selection: Mapped[str] = mapped_column(String(50), nullable=False)  # total: under / over
    odds: Mapped[float] = mapped_column(nullable=False)  # 下注时赔率
    stake: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)  # 投注金额
    potential_payout: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)  # 预期赔付
    actual_payout: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=0)  # 实际赔付
    # 盘口线：大小球 total / 让球 spread（结算用）
    line: Mapped[Optional[float]] = mapped_column(nullable=True)

    # 多站点
    provider: Mapped[str] = mapped_column(String(50), default="OB体育", index=True)

    # 状态
    status: Mapped[BetStatus] = mapped_column(default=BetStatus.SUCCESS, nullable=False, index=True)

    # AI标记
    is_ai_bet: Mapped[bool] = mapped_column(Boolean, default=False)
    ai_confidence: Mapped[Optional[float]] = mapped_column(nullable=True)
    ai_reasoning: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # LLM分析理由
    # 外部真实注单号（OB orderNo 等）
    external_bet_id: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    # 结算时间（NULL=未结算；由 bet_settlement 按完场比分写回）
    settled_at: Mapped[Optional[datetime]] = mapped_column(DateTimeUTC, nullable=True)

    # 时间戳
    created_at: Mapped[datetime] = mapped_column(DateTimeUTC, default=lambda: datetime.now(timezone.utc), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTimeUTC, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    # 关系
    user: Mapped[User] = relationship("User", back_populates="bets")
    match: Mapped[Match] = relationship("Match")

    __table_args__ = (
        Index("idx_bet_user_status", "user_id", "status"),
        Index("idx_bet_match", "match_id"),
    )


# === 博彩站点账户 ===
class BookmakerAccount(Base):
    __tablename__ = "bookmaker_accounts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)

    code: Mapped[str] = mapped_column(String(32), nullable=False, index=True)  # ob/pinnacle
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    base_url: Mapped[str] = mapped_column(String(500), default="")
    username: Mapped[str] = mapped_column(String(100), default="")
    password_encrypted: Mapped[str] = mapped_column(Text, default="")
    session_token_encrypted: Mapped[str] = mapped_column(Text, default="")
    profile_json: Mapped[dict] = mapped_column(JSON, default=dict)

    status: Mapped[BookmakerStatus] = mapped_column(default=BookmakerStatus.DISCONNECTED, nullable=False)
    balance: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_sync_at: Mapped[Optional[datetime]] = mapped_column(DateTimeUTC, nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTimeUTC, default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(
        DateTimeUTC,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    user: Mapped[User] = relationship("User", back_populates="bookmaker_accounts")

    __table_args__ = (
        UniqueConstraint("user_id", "code", name="uq_user_bookmaker"),
        Index("idx_bm_user_status", "user_id", "status"),
    )


# === 赛前上下文持久化 ===
class MatchContextRow(Base):
    __tablename__ = "match_contexts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    match_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("matches.id", ondelete="CASCADE"), nullable=True, index=True
    )
    fixture_key: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="none")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    quality: Mapped[dict] = mapped_column(JSON, default=dict)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTimeUTC, default=lambda: datetime.now(timezone.utc), index=True
    )
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTimeUTC, nullable=True)

    __table_args__ = (
        UniqueConstraint("fixture_key", name="uq_match_ctx_fixture_key"),
        Index("idx_match_ctx_match", "match_id"),
    )


# === 流水/交易记录 ===
class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)

    type: Mapped[TransactionType] = mapped_column(nullable=False, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    balance_after: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)  # 变动后余额

    # 关联
    bet_id: Mapped[Optional[int]] = mapped_column(ForeignKey("bets.id", ondelete="SET NULL"), nullable=True)
    description: Mapped[str] = mapped_column(String(500), default="")

    # 时间戳
    created_at: Mapped[datetime] = mapped_column(DateTimeUTC, default=lambda: datetime.now(timezone.utc), index=True)

    user: Mapped[User] = relationship("User", back_populates="transactions")

    __table_args__ = (
        Index("idx_tx_user_type", "user_id", "type"),
        Index("idx_tx_created", "created_at"),
    )
