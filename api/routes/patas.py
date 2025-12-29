"""PATAS Anti-Spam detection endpoint with billing integration."""

import httpx
import secrets
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, HTTPException, Header, Depends, status, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from api.services.usage_tracker_v2 import UsageTracker
from api.services.free_tier_service import FreeTierService
from api.services.credits_service import usd_to_credits, PRODUCT_CREDITS
from api.db.base import get_db
from api.dependencies import get_redis

router = APIRouter(prefix="/api/v1/patas", tags=["patas"])

# PATAS API URL (placeholder - will be deployed separately)
PATAS_API = "https://patas.kikuai.dev"

# Pricing: 5 credits per 100 messages = $0.005 per 100 = $0.00005 per message
COST_PER_MESSAGE = Decimal("0.00005")
CREDITS_PER_100_MESSAGES = 5


class AnalyzeRequest(BaseModel):
    """Request schema for spam analysis."""
    
    messages: list[str] = Field(..., description="List of messages to analyze", min_length=1, max_length=1000)
    context: Optional[str] = Field(default=None, description="Optional context for analysis")
    threshold: float = Field(default=0.7, ge=0.0, le=1.0, description="Spam detection threshold")


class MessageResult(BaseModel):
    """Result for a single message."""
    
    text: str
    is_spam: bool
    confidence: float
    reasons: list[str] = []


class AnalyzeResponse(BaseModel):
    """Response schema for spam analysis."""
    
    results: list[MessageResult]
    spam_count: int
    total_count: int
    generated_rules: Optional[list[str]] = None
    billing: Optional[dict] = None
    free_tier: Optional[dict] = None


@router.post("/analyze", response_model=AnalyzeResponse)
async def analyze_messages(
    request: AnalyzeRequest,
    response: Response,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    x_forwarded_for: Optional[str] = Header(None, alias="X-Forwarded-For"),
    cf_connecting_ip: Optional[str] = Header(None, alias="CF-Connecting-IP"),
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
):
    """
    Analyze messages for spam with auto-generated blocking rules.
    
    **Authentication:**
    - Without API key: 100 messages per day (10,000/month) per IP
    - With API key: Uses credit balance (5 credits per 100 messages)
    
    **Headers returned:**
    - X-Credits-Used: Credits consumed by this request
    - X-Credits-Balance: Remaining balance after request
    
    **Responses:**
    - 200: Analysis successful
    - 402: Insufficient credits
    - 429: Free tier limit exceeded
    - 503: PATAS service unavailable
    """
    # Get client IP
    client_ip = cf_connecting_ip or (
        x_forwarded_for.split(",")[0].strip() if x_forwarded_for else "unknown"
    )
    
    message_count = len(request.messages)
    
    # Check authentication
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
        cost = COST_PER_MESSAGE * message_count
        credits_required = (message_count / 100) * CREDITS_PER_100_MESSAGES
        
        if account.balance_usd < cost:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail={
                    "code": "INSUFFICIENT_CREDITS",
                    "message": f"Insufficient credits. Required: {credits_required:.1f} credits for {message_count} messages",
                    "balance_credits": usd_to_credits(account.balance_usd),
                    "required_credits": credits_required,
                    "message_count": message_count,
                    "topup_url": "https://kikuai.dev/pricing",
                },
            )
    else:
        # Anonymous: Check free tier (per message)
        free_tier_result = await free_tier_service.check_limit("patas", client_ip, units=message_count)
        
        if not free_tier_result.allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "code": "FREE_LIMIT_EXCEEDED",
                    "message": f"Free tier limit exceeded. You tried to analyze {message_count} messages.",
                    "remaining_today": free_tier_result.remaining_daily,
                    "remaining_month": free_tier_result.remaining_monthly,
                    "limit_daily": free_tier_result.limit_daily,
                    "limit_monthly": free_tier_result.limit_monthly,
                    "resets_at": free_tier_result.resets_at_daily,
                    "signup_url": "https://kikuai.dev/auth/login",
                    "pricing_url": "https://kikuai.dev/pricing",
                },
            )
    
    # Forward to PATAS API (or mock for now)
    try:
        # TODO: Replace with actual PATAS API call when deployed
        # For now, return mock results
        results = []
        spam_count = 0
        
        for msg in request.messages:
            # Simple mock detection (replace with real API)
            is_spam = any(word in msg.lower() for word in ["crypto", "investment", "click here", "free money"])
            confidence = 0.95 if is_spam else 0.1
            
            results.append(MessageResult(
                text=msg[:100],  # Truncate for response
                is_spam=is_spam,
                confidence=confidence,
                reasons=["keyword_match"] if is_spam else [],
            ))
            
            if is_spam:
                spam_count += 1
        
        # Generate mock rules
        generated_rules = None
        if spam_count > 0:
            generated_rules = [
                f"BLOCK: contains 'crypto' or 'investment'",
                f"BLOCK: links to unknown domains",
            ]
        
    except httpx.RequestError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "SERVICE_UNAVAILABLE", "message": f"PATAS service unavailable: {e}"},
        )
    
    # Build response
    result = AnalyzeResponse(
        results=results,
        spam_count=spam_count,
        total_count=message_count,
        generated_rules=generated_rules,
    )
    
    # Deduct credits on success
    if account:
        tracker = UsageTracker(db)
        idempotency_key = f"patas_{account.id}_{secrets.token_hex(8)}"
        
        new_balance = await tracker.track_usage(
            telegram_id=account.telegram_id,
            product_id="patas",
            idempotency_key=idempotency_key,
            units=message_count,
            metadata={
                "endpoint": "analyze",
                "message_count": message_count,
                "spam_count": spam_count,
            },
        )
        
        # Set billing headers
        credits_used = (message_count / 100) * CREDITS_PER_100_MESSAGES
        credits_remaining = usd_to_credits(new_balance)
        response.headers["X-Credits-Used"] = f"{credits_used:.1f}"
        response.headers["X-Credits-Balance"] = str(credits_remaining)
        
        result.billing = {
            "credits_used": credits_used,
            "credits_remaining": credits_remaining,
            "messages_analyzed": message_count,
        }
    else:
        # Record free tier usage
        await free_tier_service.record_usage("patas", client_ip, units=message_count)
        
        remaining = await free_tier_service.get_remaining("patas", client_ip)
        result.free_tier = remaining
    
    return result
