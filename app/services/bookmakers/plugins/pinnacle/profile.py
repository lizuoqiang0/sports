"""平博站点画像。"""
from __future__ import annotations

from typing import Any

PROFILE: dict[str, Any] = {
    "code": "pinnacle",
    "name": "平博",
    "auth_mode": "session",
    "portal": False,
    "manual_venue": False,
    "default_url": "https://www.rowilong.com",
    "default_balance": 0.0,
    "login_paths": [
        "/",
        "/zh-cn/account/login",
        "/zh-cn/login",
        "/account/login",
        "/login",
        "/zh-cn/",
    ],
    "sports_paths": ["/"],
    "venue_labels": ("平博", "Pinnacle", "体育"),
    "token_storage_keys": [
        "access_token", "accessToken", "token", "Token",
        "Authorization", "authToken", "jwt",
    ],
    "odds_url_hints": (
        "odds", "sports-service", "member-service", "fixture",
        "league", "matchup", "soccer", "basketball",
        "compact", "guest", "allodds",
    ),
    "preferred_odds_format": "european",
}
