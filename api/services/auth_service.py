"""Authentication service with JWT tokens.

Supports:
- Magic Link authentication (email)
- Telegram OAuth authentication
- JWT access + refresh tokens
"""

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple
from uuid import UUID

import jwt
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.base import Account
from config.settings import SERVER_SECRET, TELEGRAM_BOT_TOKEN

# JWT Configuration
JWT_SECRET = SERVER_SECRET
JWT_ALGORITHM = "HS256"
JWT_ACCESS_EXPIRY_MINUTES = 15
JWT_REFRESH_EXPIRY_DAYS = 7
MAGIC_LINK_EXPIRY_MINUTES = 15


class TokenPair(BaseModel):
    """Access and refresh token pair."""
    access_token: str
    refresh_token: str
    expires_in: int  # seconds until access token expires
    token_type: str = "Bearer"


class UserInfo(BaseModel):
    """User information from token."""
    account_id: str
    telegram_id: Optional[int] = None
    email: Optional[str] = None


class AuthService:
    """Authentication service for dashboard access."""
    
    @staticmethod
    def generate_magic_token() -> str:
        """Generate a secure magic link token."""
        return secrets.token_urlsafe(32)
    
    @staticmethod
    def create_access_token(account_id: UUID, telegram_id: Optional[int] = None) -> str:
        """Create a JWT access token."""
        payload = {
            "sub": str(account_id),
            "tid": telegram_id,
            "type": "access",
            "exp": datetime.now(timezone.utc) + timedelta(minutes=JWT_ACCESS_EXPIRY_MINUTES),
            "iat": datetime.now(timezone.utc),
        }
        return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    
    @staticmethod
    def create_refresh_token(account_id: UUID) -> Tuple[str, str]:
        """Create a refresh token and return (token, token_hash)."""
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        return token, token_hash
    
    @staticmethod
    def hash_refresh_token(token: str) -> str:
        """Hash a refresh token for storage."""
        return hashlib.sha256(token.encode()).hexdigest()
    
    @staticmethod
    def create_token_pair(account: Account) -> Tuple[TokenPair, str]:
        """Create access + refresh token pair.
        
        Returns:
            Tuple of (TokenPair, refresh_token_hash for storage)
        """
        access_token = AuthService.create_access_token(account.id, account.telegram_id)
        refresh_token, refresh_hash = AuthService.create_refresh_token(account.id)
        
        token_pair = TokenPair(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=JWT_ACCESS_EXPIRY_MINUTES * 60,
        )
        
        return token_pair, refresh_hash
    
    @staticmethod
    def verify_access_token(token: str) -> Optional[UserInfo]:
        """Verify and decode an access token."""
        try:
            payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
            
            if payload.get("type") != "access":
                return None
            
            return UserInfo(
                account_id=payload["sub"],
                telegram_id=payload.get("tid"),
            )
        except jwt.ExpiredSignatureError:
            return None
        except jwt.InvalidTokenError:
            return None
    
    @staticmethod
    def validate_telegram_auth(auth_data: dict) -> bool:
        """Validate Telegram Login Widget authentication data.
        
        Telegram sends: id, first_name, last_name, username, photo_url, auth_date, hash
        We verify the hash using bot token.
        """
        import logging
        logger = logging.getLogger("api.auth")
        
        logger.info(f"[TG Auth] Validating auth data: {list(auth_data.keys())}")
        
        if not TELEGRAM_BOT_TOKEN:
            logger.error("[TG Auth] TELEGRAM_BOT_TOKEN not set!")
            return False
        
        # Make a copy to avoid mutating original
        data = auth_data.copy()
        received_hash = data.pop("hash", None)
        if not received_hash:
            logger.error("[TG Auth] No hash in auth data")
            return False
        
        # Check auth_date is not too old (24 hours)
        auth_date = data.get("auth_date")
        if auth_date:
            try:
                auth_timestamp = int(auth_date)
                age = datetime.now(timezone.utc).timestamp() - auth_timestamp
                logger.info(f"[TG Auth] auth_date age: {age:.0f}s")
                if age > 86400:
                    logger.error(f"[TG Auth] auth_date too old: {age:.0f}s > 86400s")
                    return False
            except ValueError:
                logger.error("[TG Auth] Invalid auth_date format")
                return False
        
        # Filter out None values (Telegram doesn't send them)
        data = {k: v for k, v in data.items() if v is not None}
        
        # Create data check string
        data_check_arr = [f"{k}={v}" for k, v in sorted(data.items())]
        data_check_string = "\n".join(data_check_arr)
        
        logger.debug(f"[TG Auth] Data check string: {data_check_string}")
        
        # Calculate expected hash
        secret_key = hashlib.sha256(TELEGRAM_BOT_TOKEN.encode()).digest()
        expected_hash = hmac.new(
            secret_key,
            data_check_string.encode(),
            hashlib.sha256
        ).hexdigest()
        
        logger.info(f"[TG Auth] Expected hash: {expected_hash[:16]}...")
        logger.info(f"[TG Auth] Received hash: {received_hash[:16]}...")
        
        match = hmac.compare_digest(expected_hash, received_hash)
        if not match:
            logger.error("[TG Auth] Hash mismatch!")
        else:
            logger.info("[TG Auth] Hash verified successfully!")
        
        return match

    
    @staticmethod
    async def get_or_create_account_by_telegram(
        db: AsyncSession,
        telegram_id: int,
        username: Optional[str] = None,
        first_name: Optional[str] = None,
    ) -> Account:
        """Get existing account or create new one for Telegram user."""
        stmt = select(Account).where(Account.telegram_id == telegram_id)
        result = await db.execute(stmt)
        account = result.scalar_one_or_none()
        
        if account:
            account.last_active_at = datetime.utcnow()
            await db.commit()
            return account
        
        # Create new account
        account = Account(
            telegram_id=telegram_id,
        )
        db.add(account)
        await db.commit()
        await db.refresh(account)
        
        return account
    
    @staticmethod
    async def get_account_by_email(db: AsyncSession, email: str) -> Optional[Account]:
        """Get account by email."""
        stmt = select(Account).where(Account.email == email)
        result = await db.execute(stmt)
        return result.scalar_one_or_none()
    
    @staticmethod
    async def get_account_by_id(db: AsyncSession, account_id: UUID) -> Optional[Account]:
        """Get account by ID."""
        stmt = select(Account).where(Account.id == account_id)
        result = await db.execute(stmt)
        return result.scalar_one_or_none()
    
    @staticmethod
    async def set_magic_link_token(db: AsyncSession, account: Account) -> str:
        """Set a magic link token for the account."""
        token = AuthService.generate_magic_token()
        account.email_auth_token = token
        account.email_auth_expires = datetime.utcnow() + timedelta(minutes=MAGIC_LINK_EXPIRY_MINUTES)
        await db.commit()
        return token
    
    @staticmethod
    async def verify_magic_link_token(db: AsyncSession, token: str) -> Optional[Account]:
        """Verify a magic link token and return the account."""
        stmt = select(Account).where(
            Account.email_auth_token == token,
            Account.email_auth_expires > datetime.utcnow()
        )
        result = await db.execute(stmt)
        account = result.scalar_one_or_none()
        
        if account:
            # Clear token after use (one-time use)
            account.email_auth_token = None
            account.email_auth_expires = None
            account.last_active_at = datetime.utcnow()
            await db.commit()
        
        return account
