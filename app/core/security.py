"""
安全模块 - JWT鉴权、密码哈希、权限控制
"""
from datetime import datetime, timedelta, timezone
from typing import Optional, List
from enum import Enum

import jwt
from passlib.context import CryptContext
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.config import settings
from app.database import get_db
from app.models.user import User, UserRole


# === 密码哈希 ===
pwd_context = CryptContext(
    schemes=["bcrypt"],
    deprecated="auto",
    bcrypt__rounds=settings.BCRYPT_ROUNDS,
)


def hash_password(password: str) -> str:
    """哈希密码"""
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """验证密码"""
    return pwd_context.verify(plain_password, hashed_password)


# === JWT Token ===
class TokenType(str, Enum):
    ACCESS = "access"
    REFRESH = "refresh"


def create_access_token(user_id: int, role: UserRole) -> str:
    """创建访问令牌"""
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {
        "sub": str(user_id),
        "role": role.value,
        "type": TokenType.ACCESS.value,
        "iat": datetime.now(timezone.utc),
        "exp": expire,
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def create_refresh_token(user_id: int) -> str:
    """创建刷新令牌"""
    expire = datetime.now(timezone.utc) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    payload = {
        "sub": str(user_id),
        "type": TokenType.REFRESH.value,
        "iat": datetime.now(timezone.utc),
        "exp": expire,
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def decode_token(token: str) -> dict:
    """解码并验证Token"""
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token已过期")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="无效Token")


# === 鉴权依赖 ===
bearer_scheme = HTTPBearer()


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """获取当前登录用户"""
    payload = decode_token(credentials.credentials)
    if payload.get("type") != TokenType.ACCESS.value:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token类型错误")

    user_id = int(payload.get("sub"))
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用户不存在")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="账户已禁用")

    return user


def require_roles(*allowed_roles: UserRole):
    """角色权限装饰器工厂"""
    async def role_checker(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"需要 {[r.value for r in allowed_roles]} 权限"
            )
        return current_user
    return role_checker


# === 速率限制 ===
from collections import defaultdict
import time

_rate_limit_store: dict[str, list[float]] = defaultdict(list)


def check_rate_limit(user_id: int, action: str, max_requests: int, window_seconds: int) -> bool:
    """进程内滑动窗口（多 worker 不共享；优先用 check_rate_limit_async）。"""
    key = f"{user_id}:{action}"
    now = time.time()
    window_start = now - window_seconds
    _rate_limit_store[key] = [t for t in _rate_limit_store[key] if t > window_start]
    if len(_rate_limit_store[key]) >= max_requests:
        return False
    _rate_limit_store[key].append(now)
    return True


async def check_rate_limit_async(
    key: str,
    action: str,
    max_requests: int,
    window_seconds: int,
) -> bool:
    """Redis 滑动计数限流（多 worker 安全）；Redis 不可用时回退进程内。"""
    rkey = f"rl:{action}:{key}"
    try:
        from app.core.cache import cache

        n = await cache.client.incr(rkey)
        if n == 1:
            await cache.client.expire(rkey, int(window_seconds))
        return int(n) <= int(max_requests)
    except Exception:
        # 回退：用哈希 key 的进程内限流
        h = abs(hash(rkey)) % (10**9)
        return check_rate_limit(h, action, max_requests, window_seconds)
