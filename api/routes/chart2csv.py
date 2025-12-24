"""Chart2CSV proxy endpoint with billing integration."""

import httpx
import secrets
from datetime import date
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, HTTPException, UploadFile, File, Header, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from api.middleware.auth import require_scope, verify_api_key_optional
from api.services.usage_tracker_v2 import UsageTracker
from api.db.base import get_db, Account
from api.dependencies import get_redis

router = APIRouter(prefix="/api/v1/chart2csv", tags=["chart2csv"])

# Chart2CSV API URL - use public endpoint (runs as separate service)
CHART2CSV_API = "https://chart2csv.kikuai.dev"

# Pricing
CREDITS_PER_EXTRACTION = Decimal("0.01")  # $0.01 per extraction
FREE_DAILY_LIMIT = 3


async def check_free_tier(ip: str, redis) -> tuple[bool, int]:
    """
    Check if IP has free tier quota remaining.
    
    Returns: (allowed: bool, remaining: int)
    """
    key = f"free:chart2csv:{ip}:{date.today().isoformat()}"
    
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, 86400)  # 24 hours
    
    remaining = max(0, FREE_DAILY_LIMIT - count)
    allowed = count <= FREE_DAILY_LIMIT
    
    return allowed, remaining


@router.post("/extract")
async def extract_chart(
    file: UploadFile = File(...),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    x_forwarded_for: Optional[str] = Header(None, alias="X-Forwarded-For"),
    db: AsyncSession = Depends(get_db),
    redis = Depends(get_redis),
):
    """
    Extract data from a chart image.
    
    **Authentication:**
    - Without API key: 3 free extractions per day per IP
    - With API key: Uses credit balance (1 credit = $0.01)
    
    **Responses:**
    - 200: Extraction successful
    - 402: Insufficient credits
    - 429: Free tier limit exceeded
    - 500: Extraction failed
    """
    # Get client IP
    client_ip = x_forwarded_for.split(",")[0].strip() if x_forwarded_for else "unknown"
    
    # Check authentication
    account = None
    if x_api_key:
        try:
            # Verify API key and get account
            from api.middleware.auth import verify_api_key
            account = await verify_api_key(x_api_key, db)
        except HTTPException:
            # Invalid key, treat as anonymous
            pass
    
    if account:
        # Authenticated: Check credits
        tracker = UsageTracker(db)
        
        # Check balance before processing
        if account.balance_usd < CREDITS_PER_EXTRACTION:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail={
                    "code": "INSUFFICIENT_CREDITS",
                    "message": f"Insufficient credits. Required: ${CREDITS_PER_EXTRACTION}, Available: ${account.balance_usd}",
                    "balance": float(account.balance_usd),
                    "required": float(CREDITS_PER_EXTRACTION),
                }
            )
    else:
        # Anonymous: Check free tier
        allowed, remaining = await check_free_tier(client_ip, redis)
        
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "code": "FREE_LIMIT_EXCEEDED",
                    "message": f"Free tier limit exceeded. Sign in to continue.",
                    "limit": FREE_DAILY_LIMIT,
                    "resets_at": f"{date.today().isoformat()}T00:00:00Z",
                }
            )
    
    # Forward to Chart2CSV API
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            # Read file content
            file_content = await file.read()
            
            # Forward request
            response = await client.post(
                f"{CHART2CSV_API}/v1/extract",
                files={"file": (file.filename, file_content, file.content_type)},
            )
            
            if response.status_code != 200:
                raise HTTPException(
                    status_code=response.status_code,
                    detail=response.json() if response.headers.get("content-type", "").startswith("application/json") else response.text
                )
            
            result = response.json()
            
    except httpx.RequestError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "SERVICE_UNAVAILABLE", "message": f"Chart2CSV service unavailable: {str(e)}"}
        )
    
    # Deduct credits on success (only for authenticated users)
    if account and result.get("success"):
        idempotency_key = f"chart2csv_{account.id}_{secrets.token_hex(8)}"
        
        await tracker.track_usage(
            telegram_id=account.telegram_id,
            product_id="chart2csv",
            idempotency_key=idempotency_key,
            units=1,
            metadata={
                "endpoint": "extract",
                "chart_type": result.get("chart_type"),
                "cost_usd": float(CREDITS_PER_EXTRACTION),
            }
        )
        
        # Add billing info to response
        result["billing"] = {
            "credits_used": float(CREDITS_PER_EXTRACTION),
            "balance_remaining": float(account.balance_usd - CREDITS_PER_EXTRACTION),
        }
    elif not account:
        # Add free tier info
        _, remaining = await check_free_tier(client_ip, redis)
        result["free_tier"] = {
            "remaining_today": remaining,
            "limit": FREE_DAILY_LIMIT,
        }
    
    return result
