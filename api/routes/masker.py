"""Masker PII redaction proxy endpoint with billing integration and Credits system."""

import httpx
import secrets
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Header, Depends, status, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from api.services.usage_tracker_v2 import UsageTracker
from api.services.free_tier_service import FreeTierService
from api.services.credits_service import usd_to_credits, PRODUCT_CREDITS
from api.db.base import get_db
from api.dependencies import get_redis

router = APIRouter(prefix="/api/v1/masker", tags=["masker"])

# Masker API URL (separate service)
MASKER_API = "https://masker.kikuai.dev"

# Pricing in USD (matches products table)
COST_PER_REQUEST = Decimal("0.001")  # 1 credit


class RedactRequest(BaseModel):
    """Request schema for redaction."""

    text: str | None = Field(default=None, description="Plain text to redact")
    json: dict[str, Any] | None = Field(default=None, description="JSON to redact")
    mode: str = Field(default="mask", description="Redaction mode: mask, placeholder, redact")
    language: str = Field(default="en", description="Language: en, ru")
    entities: list[str] | None = Field(default=None, description="Entity types to redact")


@router.post("/redact")
async def redact_pii(
    request: RedactRequest,
    response: Response,
    x_api_key: str | None = Header(None, alias="X-API-Key"),
    x_forwarded_for: str | None = Header(None, alias="X-Forwarded-For"),
    cf_connecting_ip: str | None = Header(None, alias="CF-Connecting-IP"),
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
):
    """
    Redact PII from text or JSON.

    **Authentication:**
    - Without API key: 100 free requests per day (2,000/month) per IP
    - With API key: Uses credit balance (1 credit per request)

    **Headers returned:**
    - X-Credits-Used: Credits consumed by this request
    - X-Credits-Balance: Remaining balance after request

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

    free_tier_service = FreeTierService(redis)

    if account:
        # Authenticated: Check credits
        credits_required = PRODUCT_CREDITS["masker"]
        
        if account.balance_usd < COST_PER_REQUEST:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail={
                    "code": "INSUFFICIENT_CREDITS",
                    "message": f"Insufficient credits. Required: {credits_required} credit, Available: {usd_to_credits(account.balance_usd)} credits",
                    "balance_credits": usd_to_credits(account.balance_usd),
                    "required_credits": credits_required,
                    "topup_url": "https://kikuai.dev/pricing",
                },
            )
    else:
        # Anonymous: Check free tier
        free_tier_result = await free_tier_service.check_limit("masker", client_ip)

        if not free_tier_result.allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "code": "FREE_LIMIT_EXCEEDED",
                    "message": "Free tier limit exceeded. Sign in to continue or purchase credits.",
                    "remaining_today": free_tier_result.remaining_daily,
                    "remaining_month": free_tier_result.remaining_monthly,
                    "limit_daily": free_tier_result.limit_daily,
                    "limit_monthly": free_tier_result.limit_monthly,
                    "resets_at": free_tier_result.resets_at_daily,
                    "signup_url": "https://kikuai.dev/auth/login",
                    "pricing_url": "https://kikuai.dev/pricing",
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

            api_response = await client.post(
                f"{MASKER_API}/api/v1/mask",
                json=body,
            )

            if api_response.status_code != 200:
                raise HTTPException(
                    status_code=api_response.status_code,
                    detail=(
                        api_response.json()
                        if api_response.headers.get("content-type", "").startswith("application/json")
                        else api_response.text
                    ),
                )

            result = api_response.json()

    except httpx.RequestError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "SERVICE_UNAVAILABLE", "message": f"Masker service unavailable: {e}"},
        )

    # Deduct credits on success (only for authenticated users)
    if account:
        tracker = UsageTracker(db)
        idempotency_key = f"masker_{account.id}_{secrets.token_hex(8)}"

        new_balance = await tracker.track_usage(
            telegram_id=account.telegram_id,
            product_id="masker",
            idempotency_key=idempotency_key,
            units=1,
            metadata={
                "endpoint": "redact",
                "mode": request.mode,
            },
        )

        # Set billing headers
        credits_used = PRODUCT_CREDITS["masker"]
        credits_remaining = usd_to_credits(new_balance)
        response.headers["X-Credits-Used"] = str(credits_used)
        response.headers["X-Credits-Balance"] = str(credits_remaining)

        result["billing"] = {
            "credits_used": credits_used,
            "credits_remaining": credits_remaining,
        }
    else:
        # Record free tier usage
        await free_tier_service.record_usage("masker", client_ip)
        
        remaining = await free_tier_service.get_remaining("masker", client_ip)
        result["free_tier"] = remaining

    return result
