"""博彩站点同步：对外稳定 re-export。"""
from __future__ import annotations

from app.services.bookmakers.match_resolve import (
    _norm_team,
    _pair_similarity,
    _team_similarity,
    _is_junk_team_name,
    _recover_teams_from_league,
    _resolve_match_id,
    _apply_score_clock,
    _naive_now,
    _parse_start,
    _sport_from_str,
)
from app.services.bookmakers.purge import (
    SUPPORTED_SPORTS,
    _is_live_football_basketball,
    maybe_run_periodic_purge,
    purge_demo_matches,
    purge_unsupported_sports,
    purge_sport_mismatches,
    purge_unknown_leagues,
    purge_virtual_matches,
)
from app.services.bookmakers.sync_full import ensure_default_accounts, sync_user_bookmakers
from app.services.bookmakers.sync_live import sync_live_scores_odds

__all__ = [
    "SUPPORTED_SPORTS",
    "ensure_default_accounts",
    "sync_user_bookmakers",
    "sync_live_scores_odds",
    "purge_demo_matches",
    "purge_unsupported_sports",
    "purge_sport_mismatches",
    "purge_unknown_leagues",
    "purge_virtual_matches",
    "maybe_run_periodic_purge",
    "_norm_team",
    "_pair_similarity",
    "_team_similarity",
    "_is_junk_team_name",
    "_recover_teams_from_league",
    "_resolve_match_id",
    "_apply_score_clock",
    "_is_live_football_basketball",
    "_naive_now",
    "_parse_start",
    "_sport_from_str",
]
