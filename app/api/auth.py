"""
鉴权 API - 注册 / 登录 / 刷新Token / 用户信息
"""
import logging
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.models.user import User, UserRole
from fastapi import Request

from app.config import settings
from app.core.security import (
    hash_password, verify_password,
    create_access_token, create_refresh_token,
    decode_token, TokenType, get_current_user,
    check_rate_limit, check_rate_limit_async,
)
from app.schemas import (
    RegisterRequest, LoginRequest, TokenResponse,
    RefreshTokenRequest, UserInfoResponse, APIResponse
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/auth", tags=["鉴权"])
bearer = HTTPBearer()


def _client_ip(request: Request) -> str:
    xff = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if xff:
        return xff
    if request.client:
        return request.client.host or "unknown"
    return "unknown"


# === 注册 ===
@router.post("/register", response_model=APIResponse)
async def register(req: RegisterRequest, request: Request, db: AsyncSession = Depends(get_db)):
    """用户注册"""
    if not settings.ALLOW_PUBLIC_REGISTER:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="当前环境已关闭公开注册")
    ip = _client_ip(request)
    if not await check_rate_limit_async(ip, "register", max_requests=5, window_seconds=3600):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="注册过于频繁，请稍后再试")
    # 检查用户名/邮箱是否已存在
    existing = await db.execute(
        select(User).where((User.username == req.username) | (User.email == req.email))
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="用户名或邮箱已存在")

    # 创建用户
    user = User(
        username=req.username,
        email=req.email,
        hashed_password=hash_password(req.password),
        role=UserRole.USER,
        balance=Decimal("0.00"),
    )
    db.add(user)
    await db.flush()

    # 生成Token
    access_token = create_access_token(user.id, user.role)
    refresh_token = create_refresh_token(user.id)

    logger.info(f"新用户注册: id={user.id}, username={user.username}")

    return APIResponse(
        message="注册成功",
        data={
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "bearer",
            "expires_in": 30 * 60,
            "user": UserInfoResponse.model_validate(user).model_dump()
        }
    )


# === 登录 ===
@router.post("/login", response_model=APIResponse)
async def login(req: LoginRequest, request: Request, db: AsyncSession = Depends(get_db)):
    """用户登录 (支持用户名或邮箱)"""
    ip = _client_ip(request)
    # 失败也计入：按 IP + 用户名限流，防爆破
    if not await check_rate_limit_async(f"{ip}:{req.username}", "login", max_requests=20, window_seconds=300):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="登录尝试过于频繁，请稍后再试")

    # 查找用户
    result = await db.execute(
        select(User).where((User.username == req.username) | (User.email == req.username))
    )
    user = result.scalar_one_or_none()

    if not user or not verify_password(req.password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")

    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="账户已被禁用")

    # 更新登录时间
    user.last_login_at = datetime.now(timezone.utc)
    await db.flush()

    # 生成Token
    access_token = create_access_token(user.id, user.role)
    refresh_token = create_refresh_token(user.id)

    logger.info(f"用户登录: id={user.id}, username={user.username}")

    return APIResponse(
        message="登录成功",
        data={
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "bearer",
            "expires_in": 30 * 60,
            "user": UserInfoResponse.model_validate(user).model_dump()
        }
    )


# === 刷新Token ===
@router.post("/refresh", response_model=APIResponse)
async def refresh_token(req: RefreshTokenRequest, db: AsyncSession = Depends(get_db)):
    """用Refresh Token换取新的Access Token"""
    payload = decode_token(req.refresh_token)

    if payload.get("type") != TokenType.REFRESH.value:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token类型错误")

    user_id = int(payload.get("sub"))
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户不存在或已禁用")

    new_access_token = create_access_token(user.id, user.role)
    new_refresh_token = create_refresh_token(user.id)

    return APIResponse(
        message="Token刷新成功",
        data={
            "access_token": new_access_token,
            "refresh_token": new_refresh_token,
            "token_type": "bearer",
            "expires_in": 30 * 60,
        }
    )


# === 当前用户信息 ===
@router.get("/me", response_model=APIResponse)
async def get_me(current_user: User = Depends(get_current_user)):
    """获取当前登录用户信息"""
    return APIResponse(data=UserInfoResponse.model_validate(current_user).model_dump())


# === 登出 ===
@router.post("/logout", response_model=APIResponse)
async def logout(current_user: User = Depends(get_current_user)):
    """登出 (客户端应清除Token)"""
    logger.info(f"用户登出: id={current_user.id}")
    return APIResponse(message="登出成功")


# === 修改密码 ===
@router.post("/change-password", response_model=APIResponse)
async def change_password(
    old_password: str,
    new_password: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """修改密码"""
    if not verify_password(old_password, current_user.hashed_password):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="原密码错误")

    if len(new_password) < 8:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="新密码至少8位")

    current_user.hashed_password = hash_password(new_password)
    await db.flush()

    return APIResponse(message="密码修改成功")
