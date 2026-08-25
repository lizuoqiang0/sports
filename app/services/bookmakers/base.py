"""博彩站点连接器接口"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional


@dataclass
class VerifyResult:
    ok: bool
    message: str = ""
    balance: Decimal = Decimal("0")
    profile: Optional[dict] = None
    session_token: Optional[str] = None  # 需持久化的新 Token（明文，由 API 加密存储）


@dataclass
class PlaceBetResult:
    ok: bool
    message: str = ""
    external_bet_id: Optional[str] = None
    balance_after: Decimal = Decimal("0")
    actual_stake: Decimal = Decimal("0")


@dataclass
class RemoteOdds:
    bet_type: str
    odds_data: dict
    spread: float = 0
    total: float = 0


@dataclass
class RemoteMatch:
    external_id: str
    sport: str
    league: str
    home_team: str
    away_team: str
    start_time: str  # ISO
    status: str = "upcoming"
    venue: str = ""
    odds_list: list[RemoteOdds] = field(default_factory=list)
    home_score: int = 0
    away_score: int = 0
    clock: str = ""  # e.g. "67:12"
    period: str = ""  # e.g. "下半场" / "地图2"
    # 采集层质量快照。保持可选，兼容旧连接器与 Gate 返回格式。
    data_quality: dict[str, Any] = field(default_factory=dict)


class BookmakerConnector(ABC):
    code: str
    name: str

    def __init__(self, base_url: str, username: str, password: str, **kwargs: Any):
        self.base_url = (base_url or "").rstrip("/")
        self.username = username or ""
        self.password = password or ""
        self.extra = kwargs

    @abstractmethod
    async def verify(self) -> VerifyResult:
        ...

    @abstractmethod
    async def fetch_balance(self) -> Decimal:
        ...

    @abstractmethod
    async def fetch_matches_odds(self, local_matches: list[dict]) -> list[RemoteMatch]:
        """基于本地比赛列表生成/拉取各站赔率视图"""
        ...

    @abstractmethod
    async def place_bet(
        self,
        *,
        match_external_id: str,
        selection: str,
        odds: float,
        stake: Decimal,
        bet_type: str = "moneyline",
        odds_data: Optional[dict] = None,
    ) -> PlaceBetResult:
        ...
