"""OB 站点画像。"""
from __future__ import annotations

from typing import Any

PROFILE: dict[str, Any] = {
    "code": "ob",
    "name": "OB体育",
    "auth_mode": "kaiyun",
    "portal": True,
    "manual_venue": True,
    "default_url": "",
    "default_balance": 0.0,
    "login_paths": ["/", "/user/login", "/#/login", "/login"],
    "sports_paths": ["/"],
    "sports_menu_texts": ("体育赛事", "体育", "Sports", "体育游戏", "球类"),
    "venue_labels": ("开云体育", "ONE体育", "熊猫体育", "OB体育", "PM体育"),
    "sports_entry_texts": ("进入游戏", "进入场馆", "立即游戏", "开始游戏"),
    "token_storage_keys": ["X-API-TOKEN"],
    "odds_url_hints": (
        "odds", "match", "sport", "yewu", "matchesPB",
        "venue/launch", "launch", "football", "basketball",
    ),
    "preferred_odds_format": "european",
}
