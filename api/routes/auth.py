"""Authentication API routes.

Endpoints:
- POST /api/v1/auth/magic-link - Request magic link email
- POST /api/v1/auth/verify - Verify magic link token
- POST /api/v1/auth/telegram - Login with Telegram
- POST /api/v1/auth/refresh - Refresh access token
- POST /api/v1/auth/logout - Logout (invalidate refresh token)
- GET /api/v1/auth/me - Get current user info
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Optional
from uuid import UUID

import redis
from fastapi import APIRouter, HTTPException, Depends, Body, Header
from pydantic import BaseModel, EmailStr
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.base import get_db, Account
from api.services.auth_service import AuthService, TokenPair, UserInfo
from config.settings import REDIS_URL

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

# Redis client for refresh token storage
_redis_client = redis.from_url(REDIS_URL)
REFRESH_TOKEN_TTL = 7 * 24 * 60 * 60  # 7 days in seconds


class MagicLinkRequest(BaseModel):
    email: EmailStr


class MagicLinkResponse(BaseModel):
    status: str
    message: str


class TelegramAuthRequest(BaseModel):
    """Telegram Login Widget auth data."""
    id: int
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    username: Optional[str] = None
    photo_url: Optional[str] = None
    auth_date: int
    hash: str


class RefreshRequest(BaseModel):
    refresh_token: str


class AccountResponse(BaseModel):
    id: str
    telegram_id: Optional[int] = None
    email: Optional[str] = None
    balance_usd: str
    created_at: datetime


def _store_refresh_token(token_hash: str, account_id: UUID) -> None:
    """Store refresh token in Redis with TTL."""
    key = f"refresh_token:{token_hash}"
    data = json.dumps({"account_id": str(account_id)})
    _redis_client.setex(key, REFRESH_TOKEN_TTL, data)


def _get_refresh_token(token_hash: str) -> Optional[dict]:
    """Get refresh token data from Redis."""
    key = f"refresh_token:{token_hash}"
    data = _redis_client.get(key)
    if not data:
        return None
    try:
        parsed = json.loads(data)
        parsed["account_id"] = UUID(parsed["account_id"])
        return parsed
    except (json.JSONDecodeError, ValueError, KeyError):
        return None


def _delete_refresh_token(token_hash: str) -> None:
    """Delete refresh token from Redis."""
    key = f"refresh_token:{token_hash}"
    _redis_client.delete(key)


async def get_current_user(
    authorization: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db)
) -> Account:
    """Dependency to get current authenticated user."""
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing authorization header")
    
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header format")
    
    token = authorization[7:]  # Remove "Bearer " prefix
    user_info = AuthService.verify_access_token(token)
    
    if not user_info:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    
    account = await AuthService.get_account_by_id(db, UUID(user_info.account_id))
    if not account:
        raise HTTPException(status_code=401, detail="Account not found")
    
    return account


@router.post("/magic-link", response_model=MagicLinkResponse)
async def request_magic_link(
    request: MagicLinkRequest,
    db: AsyncSession = Depends(get_db)
):
    """Request a magic link for email-based login.
    
    Creates a new account if email is not registered (enables email-only registration).
    Sends a magic link to the email address.
    """
    from api.services.email_service import send_magic_link_email
    
    # Get or create account (enables email-only registration)
    account = await AuthService.get_or_create_account_by_email(db, request.email)
    
    token = await AuthService.set_magic_link_token(db, account)
    magic_link = f"https://kikuai.dev/auth/verify?token={token}"
    
    # Send email via Brevo
    email_sent = await send_magic_link_email(request.email, magic_link)
    if email_sent:
        logger.info(f"Magic link sent to {request.email}")
    else:
        logger.warning(f"Failed to send magic link to {request.email}, link: {magic_link}")
    
    return MagicLinkResponse(
        status="success",
        message="A magic link has been sent to your email."
    )


@router.post("/verify", response_model=TokenPair)
async def verify_magic_link(
    token: str = Body(..., embed=True),
    db: AsyncSession = Depends(get_db)
):
    """Verify a magic link token and return JWT tokens."""
    account = await AuthService.verify_magic_link_token(db, token)
    
    if not account:
        raise HTTPException(status_code=400, detail="Invalid or expired magic link")
    
    # Create token pair
    token_pair, refresh_hash = AuthService.create_token_pair(account)

    # Store refresh token in Redis
    _store_refresh_token(refresh_hash, account.id)

    return token_pair


@router.post("/telegram", response_model=TokenPair)
async def login_with_telegram(
    auth_data: TelegramAuthRequest,
    db: AsyncSession = Depends(get_db)
):
    """Login with Telegram Login Widget.
    
    Validates the auth data from Telegram and returns JWT tokens.
    Creates account if not exists.
    """
    # Validate Telegram auth
    auth_dict = auth_data.model_dump()
    if not AuthService.validate_telegram_auth(auth_dict):
        raise HTTPException(status_code=401, detail="Invalid Telegram authentication")
    
    # Get or create account
    account = await AuthService.get_or_create_account_by_telegram(
        db,
        telegram_id=auth_data.id,
        username=auth_data.username,
        first_name=auth_data.first_name,
    )
    
    # Create token pair
    token_pair, refresh_hash = AuthService.create_token_pair(account)

    # Store refresh token in Redis
    _store_refresh_token(refresh_hash, account.id)

    return token_pair


@router.post("/refresh", response_model=TokenPair)
async def refresh_access_token(
    request: RefreshRequest,
    db: AsyncSession = Depends(get_db)
):
    """Refresh an access token using a refresh token."""
    token_hash = AuthService.hash_refresh_token(request.refresh_token)

    token_data = _get_refresh_token(token_hash)
    if not token_data:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")

    # Get account
    account = await AuthService.get_account_by_id(db, token_data["account_id"])
    if not account:
        _delete_refresh_token(token_hash)
        raise HTTPException(status_code=401, detail="Account not found")

    # Rotate refresh token (delete old, create new)
    _delete_refresh_token(token_hash)

    # Create new token pair
    new_token_pair, new_refresh_hash = AuthService.create_token_pair(account)

    # Store new refresh token in Redis
    _store_refresh_token(new_refresh_hash, account.id)

    return new_token_pair


@router.post("/logout")
async def logout(
    request: RefreshRequest,
):
    """Logout by invalidating the refresh token."""
    token_hash = AuthService.hash_refresh_token(request.refresh_token)
    _delete_refresh_token(token_hash)
    return {"status": "success", "message": "Logged out successfully"}


@router.get("/me", response_model=AccountResponse)
async def get_current_account(
    account: Account = Depends(get_current_user)
):
    """Get current authenticated user's account info."""
    return AccountResponse(
        id=str(account.id),
        telegram_id=account.telegram_id,
        email=account.email,
        balance_usd=str(account.balance_usd),
        created_at=account.created_at,
    )
