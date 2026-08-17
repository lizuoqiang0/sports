"""线上真实场景门禁：禁止演示 URL 和模拟执行。"""
from __future__ import annotations

from urllib.parse import urlparse

from fastapi import HTTPException


def _is_demo_url(base_url: str) -> bool:
    """与 plugins.ob.kaiyun.is_demo_url 对齐，避免循环 import。"""
    host = (urlparse((base_url or "").strip()).hostname or "").lower()
    return (not host) or host.endswith(".demo") or "ob-sports.demo" in host


def force_live_mode() -> bool:
    """项目仅支持真实线上执行。"""
    return True


def reject_demo_url(base_url: str, *, field: str = "网址") -> None:
    if not force_live_mode():
        return
    url = (base_url or "").strip()
    if not url or _is_demo_url(url):
        raise HTTPException(
            status_code=400,
            detail=f"线上环境禁止演示站：请填写真实{field}（不可使用 .demo）",
        )


def reject_simulation(action: str = "模拟") -> None:
    if force_live_mode():
        raise HTTPException(
            status_code=403,
            detail=f"线上环境禁止{action}，仅允许真实下单/真实采数",
        )


def ensure_live_connector_url(code: str, base_url: str) -> None:
    if not force_live_mode():
        return
    url = (base_url or "").strip()
    if not url or _is_demo_url(url):
        raise ValueError(f"线上环境站点 {code} 必须配置真实网址，当前为演示/空地址")
