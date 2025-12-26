"""Masker PII redaction proxy endpoint with billing integration."""

import httpx
import secrets
from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Header, Depends, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from api.services.usage_tracker_v2 import UsageTracker
from api.db.base import get_db
from api.dependencies import get_redis

router = APIRouter(prefix="/api/v1/masker", tags=["masker"])

# Masker API URL (separate service)
MASKER_API = "https://masker.kikuai.dev"

# Pricing - $0.001 per request (1000 requests = $1)
CREDITS_PER_REQUEST = Decimal("0.001")
FREE_DAILY_LIMIT = 3


class RedactRequest(BaseModel):
    """Request schema for redaction."""

    text: str | None = Field(default=None, description="Plain text to redact")
    json: dict[str, Any] | None = Field(default=None, description="JSON to redact")
    mode: str = Field(default="mask", description="Redaction mode: mask, placeholder, redact")
    language: str = Field(default="en", description="Language: en, ru")
    entities: list[str] | None = Field(default=None, description="Entity types to redact")


async def check_free_tier(ip: str, redis) -> tuple[bool, int]:
    """
    Check if IP has free tier quota remaining.

    Returns: (allowed: bool, remaining: int)
    """
    key = f"free:masker:{ip}:{date.today().isoformat()}"

    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, 86400)  # 24 hours

    remaining = max(0, FREE_DAILY_LIMIT - count)
    allowed = count <= FREE_DAILY_LIMIT

    return allowed, remaining


@router.post("/redact")
async def redact_pii(
    request: RedactRequest,
    x_api_key: str | None = Header(None, alias="X-API-Key"),
    x_forwarded_for: str | None = Header(None, alias="X-Forwarded-For"),
    cf_connecting_ip: str | None = Header(None, alias="CF-Connecting-IP"),
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
):
    """
    Redact PII from text or JSON.

    **Authentication:**
    - Without API key: 3 free requests per day per IP
    - With API key: Uses credit balance ($0.001 per request)

    **Responses:**
    - 200: Redaction successful
    - 402: Insufficient credits
    - 429: Free tier limit exceeded
    - 503: Masker service unavailable
    """
    # Get client IP
    client_ip = cf_connecting_ip or (
        x_forwarded_for.split(",")[0].strip() if x_forwarded_for else "unknown"
    )

    # Check authentication (optional)
    account = None
    if x_api_key:
        try:
            from api.services.account_service import AccountService

            account_service = AccountService(db)
            account, _ = await account_service.verify_key(x_api_key)
        except Exception:
            pass

    if account:
        # Authenticated: Check credits
        if account.balance_usd < CREDITS_PER_REQUEST:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail={
                    "code": "INSUFFICIENT_CREDITS",
                    "message": f"Insufficient credits. Required: ${CREDITS_PER_REQUEST}, Available: ${account.balance_usd}",
                    "balance": float(account.balance_usd),
                    "required": float(CREDITS_PER_REQUEST),
                },
            )
    else:
        # Anonymous: Check free tier
        allowed, remaining = await check_free_tier(client_ip, redis)

        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "code": "FREE_LIMIT_EXCEEDED",
                    "message": "Free tier limit exceeded. Sign in to continue.",
                    "limit": FREE_DAILY_LIMIT,
                    "resets_at": f"{date.today().isoformat()}T00:00:00Z",
                },
            )

    # Forward to Masker API
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Build request body
            body = {
                "mode": request.mode,
                "language": request.language,
            }
            if request.text:
                body["text"] = request.text
            if request.json:
                body["json"] = request.json
            if request.entities:
                body["entities"] = request.entities

            response = await client.post(
                f"{MASKER_API}/api/v1/mask",
                json=body,
            )

            if response.status_code != 200:
                raise HTTPException(
                    status_code=response.status_code,
                    detail=(
                        response.json()
                        if response.headers.get("content-type", "").startswith("application/json")
                        else response.text
                    ),
                )

            result = response.json()

    except httpx.RequestError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "SERVICE_UNAVAILABLE", "message": f"Masker service unavailable: {e}"},
        )

    # Deduct credits on success (only for authenticated users)
    if account:
        tracker = UsageTracker(db)
        idempotency_key = f"masker_{account.id}_{secrets.token_hex(8)}"

        await tracker.track_usage(
            telegram_id=account.telegram_id,
            product_id="masker",
            idempotency_key=idempotency_key,
            units=1,
            metadata={
                "endpoint": "redact",
                "mode": request.mode,
                "cost_usd": float(CREDITS_PER_REQUEST),
            },
        )

        # Add billing info to response
        result["billing"] = {
            "credits_used": float(CREDITS_PER_REQUEST),
            "balance_remaining": float(account.balance_usd - CREDITS_PER_REQUEST),
        }
    else:
        # Add free tier info
        _, remaining = await check_free_tier(client_ip, redis)
        result["free_tier"] = {
            "remaining_today": remaining,
            "limit": FREE_DAILY_LIMIT,
        }

    return result
