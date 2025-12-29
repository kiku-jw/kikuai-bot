"""
Free Tier Service - Track daily and monthly usage limits.

Supports both authenticated (by account_id) and anonymous (by IP) tracking.
Uses Redis for fast counter operations with automatic expiry.
"""

import logging
from datetime import date, datetime
from typing import Optional, NamedTuple
from decimal import Decimal

import redis.asyncio as aioredis

from config.settings import REDIS_URL

logger = logging.getLogger(__name__)

# Redis client for free tier tracking
_redis_client: Optional[aioredis.Redis] = None


async def get_free_tier_redis() -> aioredis.Redis:
    """Get or create Redis client for free tier tracking."""
    global _redis_client
    if _redis_client is None:
        _redis_client = aioredis.from_url(REDIS_URL, decode_responses=True)
    return _redis_client


class FreeTierLimits(NamedTuple):
    """Daily and monthly limits for a product."""
    daily: int
    monthly: int


class FreeTierResult(NamedTuple):
    """Result of free tier limit check."""
    allowed: bool
    remaining_daily: int
    remaining_monthly: int
    limit_daily: int
    limit_monthly: int
    resets_at_daily: str  # ISO datetime
    resets_at_monthly: str  # ISO datetime


# Free tier limits per product (from spec)
FREE_TIER_LIMITS: dict[str, FreeTierLimits] = {
    "chart2csv": FreeTierLimits(daily=3, monthly=50),
    "masker": FreeTierLimits(daily=100, monthly=2000),
    "patas": FreeTierLimits(daily=100, monthly=10000),  # messages
    "reliapi": FreeTierLimits(daily=1000, monthly=10000),
}


class FreeTierService:
    """
    Service for tracking free tier usage with daily and monthly limits.
    
    Usage:
        service = FreeTierService()
        result = await service.check_limit("chart2csv", identifier="1.2.3.4")
        if result.allowed:
            await service.record_usage("chart2csv", identifier="1.2.3.4")
    """
    
    def __init__(self, redis: Optional[aioredis.Redis] = None):
        self._redis = redis
    
    async def _get_redis(self) -> aioredis.Redis:
        if self._redis:
            return self._redis
        return await get_free_tier_redis()
    
    def _get_limits(self, product_id: str) -> FreeTierLimits:
        """Get limits for a product, with fallback defaults."""
        return FREE_TIER_LIMITS.get(product_id, FreeTierLimits(daily=10, monthly=100))
    
    def _daily_key(self, product_id: str, identifier: str) -> str:
        """Redis key for daily counter."""
        today = date.today().isoformat()
        return f"free:{product_id}:{identifier}:daily:{today}"
    
    def _monthly_key(self, product_id: str, identifier: str) -> str:
        """Redis key for monthly counter."""
        month = datetime.utcnow().strftime("%Y-%m")
        return f"free:{product_id}:{identifier}:monthly:{month}"
    
    def _daily_reset_time(self) -> str:
        """ISO datetime when daily limit resets (next midnight UTC)."""
        tomorrow = date.today().isoformat()
        return f"{tomorrow}T00:00:00Z"
    
    def _monthly_reset_time(self) -> str:
        """ISO datetime when monthly limit resets (first of next month UTC)."""
        now = datetime.utcnow()
        if now.month == 12:
            next_month = now.replace(year=now.year + 1, month=1, day=1)
        else:
            next_month = now.replace(month=now.month + 1, day=1)
        return next_month.strftime("%Y-%m-%dT00:00:00Z")
    
    async def check_limit(
        self,
        product_id: str,
        identifier: str,
        units: int = 1
    ) -> FreeTierResult:
        """
        Check if free tier limit allows this usage.
        
        Args:
            product_id: Product identifier (chart2csv, masker, etc.)
            identifier: User identifier (IP for anonymous, account_id for authenticated)
            units: Number of units being consumed (default 1)
        
        Returns:
            FreeTierResult with allowed status and remaining limits
        """
        redis = await self._get_redis()
        limits = self._get_limits(product_id)
        
        daily_key = self._daily_key(product_id, identifier)
        monthly_key = self._monthly_key(product_id, identifier)
        
        # Get current counts
        daily_count = await redis.get(daily_key)
        monthly_count = await redis.get(monthly_key)
        
        daily_used = int(daily_count) if daily_count else 0
        monthly_used = int(monthly_count) if monthly_count else 0
        
        remaining_daily = max(0, limits.daily - daily_used)
        remaining_monthly = max(0, limits.monthly - monthly_used)
        
        # Check if allowed
        allowed = (
            daily_used + units <= limits.daily and
            monthly_used + units <= limits.monthly
        )
        
        return FreeTierResult(
            allowed=allowed,
            remaining_daily=remaining_daily,
            remaining_monthly=remaining_monthly,
            limit_daily=limits.daily,
            limit_monthly=limits.monthly,
            resets_at_daily=self._daily_reset_time(),
            resets_at_monthly=self._monthly_reset_time(),
        )
    
    async def record_usage(
        self,
        product_id: str,
        identifier: str,
        units: int = 1
    ) -> tuple[int, int]:
        """
        Record usage and increment counters.
        
        Returns:
            Tuple of (new_daily_count, new_monthly_count)
        """
        redis = await self._get_redis()
        
        daily_key = self._daily_key(product_id, identifier)
        monthly_key = self._monthly_key(product_id, identifier)
        
        # Increment with pipeline for atomicity
        pipe = redis.pipeline()
        pipe.incrby(daily_key, units)
        pipe.incrby(monthly_key, units)
        
        # Set expiry (daily: 48h, monthly: 35 days - generous buffer)
        pipe.expire(daily_key, 172800)  # 48 hours
        pipe.expire(monthly_key, 3024000)  # 35 days
        
        results = await pipe.execute()
        
        return int(results[0]), int(results[1])
    
    async def get_remaining(
        self,
        product_id: str,
        identifier: str
    ) -> dict:
        """
        Get remaining free tier usage for display.
        
        Returns dict with:
            - used_today
            - limit_today
            - used_month
            - limit_month
            - resets_at
        """
        result = await self.check_limit(product_id, identifier, units=0)
        limits = self._get_limits(product_id)
        
        return {
            "used_today": limits.daily - result.remaining_daily,
            "limit_today": limits.daily,
            "used_month": limits.monthly - result.remaining_monthly,
            "limit_month": limits.monthly,
            "resets_at": result.resets_at_daily,
        }
    
    async def get_all_remaining(self, identifier: str) -> dict[str, dict]:
        """Get remaining limits for all products."""
        return {
            product_id: await self.get_remaining(product_id, identifier)
            for product_id in FREE_TIER_LIMITS.keys()
        }
