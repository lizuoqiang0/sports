from decimal import Decimal

from app.services.bet_settlement import _decide_total_outcome, _split_stake


def test_total_outcomes_only_settle_under() -> None:
    assert _decide_total_outcome(selection="under", line=2.5, total=2) == "won"
    assert _decide_total_outcome(selection="under", line=2.5, total=3) == "lost"
    assert _decide_total_outcome(selection="over", line=2.5, total=3) == "unknown"
    assert _decide_total_outcome(selection="invalid", line=2.5, total=2) == "unknown"


def test_quarter_line_split_preserves_cent_stake() -> None:
    parts = _split_stake(Decimal("1.01"), 2)

    assert parts == (Decimal("0.51"), Decimal("0.50"))
    assert sum(parts) == Decimal("1.01")
