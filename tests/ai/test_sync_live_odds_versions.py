"""滚球同步的赔率版本收敛测试。"""
from datetime import datetime, timedelta
from types import SimpleNamespace

from app.models.user import BetType
from app.services.bookmakers.sync_live import _collapse_active_odds_rows


def test_active_odds_collapse_keeps_newest_version_per_market():
    now = datetime(2026, 8, 25, 9, 30, 0)
    old_total = SimpleNamespace(bet_type=BetType.TOTAL, valid_from=now - timedelta(minutes=2), valid_to=None)
    latest_total = SimpleNamespace(bet_type=BetType.TOTAL, valid_from=now - timedelta(minutes=1), valid_to=None)
    moneyline = SimpleNamespace(bet_type=BetType.MONEYLINE, valid_from=now, valid_to=None)

    kept = _collapse_active_odds_rows([latest_total, old_total, moneyline], now)

    assert kept[BetType.TOTAL] is latest_total
    assert latest_total.valid_to is None
    assert old_total.valid_to == now
    assert kept[BetType.MONEYLINE] is moneyline
