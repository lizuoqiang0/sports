from datetime import datetime, timedelta, timezone

from app.services.nowscore_evidence import build_total_market_evidence, evidence_gate_reason


def _matches(totals: list[int]) -> list[dict]:
    return [
        {"home_goals": total, "away_goals": 0, "date": f"2026-08-{index + 1:02d}"}
        for index, total in enumerate(totals)
    ]


def _context(*, sport: str = "football", fetched_at: str | None = None) -> dict:
    return {
        "source": "nowscore",
        "sport": sport,
        "identity": {"validated": True, "pair_score": 1.0},
        "fetched_at": fetched_at or datetime.now(timezone.utc).isoformat(),
        "h2h": {"matches": _matches([3, 4, 2])},
        "home_form": {"matches": _matches([3, 4, 2, 5, 3])},
        "away_form": {"matches": _matches([4, 3, 1, 4, 3])},
    }


def test_exact_quarter_line_is_used_without_rounding():
    evidence = build_total_market_evidence(_context(), line=2.25, sport="football")

    assert evidence["usable"] is True
    assert evidence["line"] == 2.25
    assert evidence["market_line_source"] == "bookmaker_live_total"
    assert evidence["buckets"]["home_form"]["over"] == 4
    assert evidence["buckets"]["home_form"]["under"] == 1
    assert evidence["consensus"]["direction"] == "over"


def test_push_is_counted_against_exact_integer_line():
    evidence = build_total_market_evidence(_context(), line=3.0, sport="football")

    home = evidence["buckets"]["home_form"]
    assert home["push"] == 2
    assert home["over"] == 2
    assert home["under"] == 1
    assert home["over_rate"] == 0.667


def test_stale_context_fails_closed():
    stale = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
    evidence = build_total_market_evidence(
        _context(fetched_at=stale), line=2.5, sport="football", max_age_sec=21600,
    )

    assert evidence["usable"] is False
    assert "context_stale_or_missing_timestamp" in evidence["warnings"]
    assert evidence_gate_reason(evidence).startswith("nowscore_evidence_unusable:")


def test_team_identity_and_sport_are_hard_requirements():
    context = _context(sport="basketball")
    context["identity"] = {"validated": False}

    evidence = build_total_market_evidence(context, line=2.5, sport="football")

    assert evidence["usable"] is False
    assert "sport_mismatch" in evidence["warnings"]
    assert "team_identity_unverified" in evidence["warnings"]


def test_basketball_rejects_football_sized_market_line():
    evidence = build_total_market_evidence(
        _context(sport="basketball"), line=2.5, sport="basketball",
    )

    assert evidence["usable"] is False
    assert "invalid_bookmaker_total_line" in evidence["warnings"]
