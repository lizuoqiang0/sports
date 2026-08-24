"""公共类型转换工具 — 全项目统一使用，消除各模块重复定义的 _to_float / _as_float。"""
from __future__ import annotations

from typing import Any, Optional


def to_float(value: Any, default: float = 0.0) -> float:
    """安全转换为 float，失败返回 default。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def to_int(value: Any, default: int = 0) -> int:
    """安全转换为 int（兼容 '3.0' 字符串），失败返回 default。"""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def to_float_or_none(value: Any) -> Optional[float]:
    """安全转换为 float，失败/空值返回 None。"""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
