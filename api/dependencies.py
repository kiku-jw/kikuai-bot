"""API Dependencies - Dependency injection for FastAPI."""

import redis
from functools import lru_cache

from config.settings import (
    REDIS_URL,
    PADDLE_ENABLED,
    PADDLE_API_KEY,
    PADDLE_WEBHOOK_SECRET,
    PADDLE_ENVIRONMENT,
    LEMONSQUEEZY_ENABLED,
    LEMONSQUEEZY_API_KEY,
    LEMONSQUEEZY_WEBHOOK_SECRET,
    TELEGRAM_BOT_TOKEN,
)
from api.services.payment_engine import (
    PaymentEngine,
    PaymentMethod,
    PaddleProvider,
    TelegramStarsProvider,
)
from api.services.lemonsqueezy_provider import LemonSqueezyProvider
from api.services.postgres_balance_manager import PostgresBalanceManager
from api.services.notification_service import TelegramNotificationService


# Redis client singleton
_redis_client = None


def get_redis_client():
    """Get Redis client singleton."""
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.from_url(REDIS_URL)
    return _redis_client


# Async Redis client for FastAPI dependencies
import redis.asyncio as aioredis

_async_redis_client = None


async def get_redis():
    """Get async Redis client for FastAPI dependency injection."""
    global _async_redis_client
    if _async_redis_client is None:
        _async_redis_client = await aioredis.from_url(REDIS_URL, decode_responses=True)
    return _async_redis_client


# Payment engine singleton
_payment_engine = None


def get_payment_engine() -> PaymentEngine:
    """
    Get or create PaymentEngine singleton.
    
    Initializes:
    - PostgresBalanceManager for balance operations (SQL Ledger)
    - TelegramNotificationService for user notifications
    - LemonSqueezyProvider for card payments (primary)
    - PaddleProvider for card payments (disabled by default)
    - TelegramStarsProvider for Stars payments
    """
    global _payment_engine
    
    if _payment_engine is None:
        redis_client = get_redis_client()
        
        # Create managers (PostgreSQL-backed for finances)
        balance_manager = PostgresBalanceManager()
        notification_service = TelegramNotificationService()
        
        # Create engine
        _payment_engine = PaymentEngine(
            balance_manager=balance_manager,
            notification_service=notification_service,
        )
        
        # Register Lemon Squeezy provider (primary)
        if LEMONSQUEEZY_ENABLED and LEMONSQUEEZY_API_KEY:
            lemonsqueezy_provider = LemonSqueezyProvider(
                api_key=LEMONSQUEEZY_API_KEY,
                webhook_secret=LEMONSQUEEZY_WEBHOOK_SECRET,
                balance_service=balance_manager,
            )
            _payment_engine.register_provider(PaymentMethod.LEMONSQUEEZY, lemonsqueezy_provider)
        
        # Register Paddle provider (disabled by default via feature flag)
        if PADDLE_ENABLED and PADDLE_API_KEY and PADDLE_WEBHOOK_SECRET:
            paddle_provider = PaddleProvider(
                api_key=PADDLE_API_KEY,
                webhook_secret=PADDLE_WEBHOOK_SECRET,
                sandbox=(PADDLE_ENVIRONMENT == "sandbox"),
            )
            _payment_engine.register_provider(PaymentMethod.PADDLE, paddle_provider)
        
        # Register Telegram Stars provider
        if TELEGRAM_BOT_TOKEN:
            stars_provider = TelegramStarsProvider(
                bot_token=TELEGRAM_BOT_TOKEN,
                redis_client=redis_client,
            )
            _payment_engine.register_provider(PaymentMethod.TELEGRAM_STARS, stars_provider)
        
        # Register Creem provider
        from config.settings import CREEM_ENABLED, CREEM_API_KEY, CREEM_PRODUCT_ID, CREEM_WEBHOOK_SECRET
        if CREEM_ENABLED and CREEM_API_KEY:
            from api.services.creem_provider import CreemProvider
            creem_provider = CreemProvider(
                api_key=CREEM_API_KEY,
                product_id=CREEM_PRODUCT_ID,
                webhook_secret=CREEM_WEBHOOK_SECRET,
                balance_service=balance_manager,
            )
            _payment_engine.register_provider(PaymentMethod.CREEM, creem_provider)
    
    return _payment_engine


def get_balance_manager() -> PostgresBalanceManager:
    """Get balance manager instance."""
    return PostgresBalanceManager()
