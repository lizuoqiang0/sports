"""风险控制引擎（规格 §11）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional


def _cfg_defaults() -> dict:
    try:
        from app.config import settings
        return {
            "max_stake_per_event": Decimal(str(settings.MAX_STAKE_PER_EVENT)),
            "max_exposure_per_source": Decimal(str(settings.MAX_EXPOSURE_PER_SOURCE)),
            "max_exposure_per_account": Decimal(str(settings.MAX_EXPOSURE_PER_ACCOUNT)),
            "max_daily_loss": Decimal(str(settings.MAX_DAILY_LOSS)),
            "min_net_profit_rate": Decimal(str(settings.MIN_NET_PROFIT_RATE)),
            "min_worst_case_profit": Decimal(str(settings.MIN_WORST_CASE_PROFIT)),
            "max_quote_age_sec": int(settings.MAX_QUOTE_AGE_SEC),
            "min_match_confidence": float(settings.MIN_MATCH_CONFIDENCE),
        }
    except Exception:
        return {
            "max_stake_per_event": Decimal("5000"),
            "max_exposure_per_source": Decimal("10000"),
            "max_exposure_per_account": Decimal("20000"),
            "max_daily_loss": Decimal("2000"),
            "min_net_profit_rate": Decimal("0.005"),
            "min_worst_case_profit": Decimal("1"),
            "max_quote_age_sec": 30,
            "min_match_confidence": 0.90,
        }


@dataclass
class RiskSettings:
    max_stake_per_event: Decimal = Decimal("5000")
    max_exposure_per_source: Decimal = Decimal("10000")
    max_exposure_per_account: Decimal = Decimal("20000")
    max_daily_loss: Decimal = Decimal("2000")
    min_net_profit_rate: Decimal = Decimal("0.005")
    min_worst_case_profit: Decimal = Decimal("1")
    max_quote_age_sec: int = 30
    min_match_confidence: float = 0.90

    @classmethod
    def from_config(cls) -> "RiskSettings":
        return cls(**_cfg_defaults())


@dataclass
class RiskContext:
    required_capital: Decimal
    worst_case_profit: Decimal
    net_profit_rate: Decimal
    match_confidence: float
    quote_ages_sec: list[float]
    source_exposures: dict[str, Decimal] = field(default_factory=dict)
    account_exposure: Decimal = Decimal("0")
    daily_loss: Decimal = Decimal("0")
    market_suspended: bool = False
    score_changed: bool = False
    balance_ok: bool = True
    source_failures: int = 0
    global_stop: bool = False
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class RiskResult:
    accepted: bool
    reasons: list[str] = field(default_factory=list)
    risk_level: str = "medium"
    checks: dict = field(default_factory=dict)


_SOURCE_FAILURES: dict[str, int] = {}


def record_source_failure(code: str) -> int:
    _SOURCE_FAILURES[code] = _SOURCE_FAILURES.get(code, 0) + 1
    return _SOURCE_FAILURES[code]


def reset_source_failure(code: str) -> None:
    _SOURCE_FAILURES[code] = 0


def source_failure_count(code: str) -> int:
    return int(_SOURCE_FAILURES.get(code, 0))


# evaluate_risk 已移除（无调用方）；风控改由 AI 策略门槛 + auto_better 执行。
