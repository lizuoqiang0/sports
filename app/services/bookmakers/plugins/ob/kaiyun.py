"""OB / 开云 helpers（演示站判定等）。"""
from __future__ import annotations

from urllib.parse import urlparse


def is_demo_url(base_url: str) -> bool:
    host = (urlparse(base_url or "").hostname or "").lower()
    return (not host) or host.endswith(".demo") or "ob-sports.demo" in host
