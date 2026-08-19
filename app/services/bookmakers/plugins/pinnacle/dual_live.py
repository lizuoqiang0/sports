"""平博双球类占位：API compact/events 已含足球+篮球，禁止后台切页抢焦点。"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def scrape_other_live_sport(main_page: Any, *, limit: int = 80) -> list:
    """占位：不抢焦点、不 goto。平博走 sports-service API 多球类，返回空。"""
    _ = main_page, limit
    logger.debug(
        "pinnacle dual-live skipped (API covers football+basketball; no sport switch)"
    )
    return []
