"""Chart2CSV proxy endpoint with billing integration and Credits system."""

import httpx
import secrets
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, HTTPException, UploadFile, File, Header, Depends, status, Response
from sqlalchemy.ext.asyncio import AsyncSession

from api.services.usage_tracker_v2 import UsageTracker
from api.services.free_tier_service import FreeTierService
from api.services.credits_service import usd_to_credits, format_credits, PRODUCT_CREDITS
from api.db.base import get_db
from api.dependencies import get_redis

router = APIRouter(prefix="/api/v1/chart2csv", tags=["chart2csv"])

# Chart2CSV API URL - use public endpoint (runs as separate service)
CHART2CSV_API = "https://chart2csv.kikuai.dev"

# Pricing in USD (matches products table)
COST_PER_EXTRACTION = Decimal("0.05")  # 50 credits


@router.post("/extract")
async def extract_chart(
    response: Response,
    file: UploadFile = File(...),
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    x_forwarded_for: Optional[str] = Header(None, alias="X-Forwarded-For"),
    cf_connecting_ip: Optional[str] = Header(None, alias="CF-Connecting-IP"),
    db: AsyncSession = Depends(get_db),
    redis = Depends(get_redis),
):
    """
    Extract data from a chart image.
    
    **Authentication:**
    - Without API key: 3 free extractions per day (50/month) per IP
    - With API key: Uses credit balance (50 credits per extraction)
    
    **Headers returned:**
    - X-Credits-Used: Credits consumed by this request
    - X-Credits-Balance: Remaining balance after request
    
    **Responses:**
    - 200: Extraction successful
    - 402: Insufficient credits
    - 429: Free tier limit exceeded
    - 500: Extraction failed
    """
    # Get client IP - prefer CF-Connecting-IP (trusted, set by Cloudflare)
    client_ip = cf_connecting_ip or (x_forwarded_for.split(",")[0].strip() if x_forwarded_for else "unknown")
    
    # Check authentication (optional)
    account = None
    if x_api_key:
        try:
            from api.services.account_service import AccountService
            account_service = AccountService(db)
            account, _ = await account_service.verify_key(x_api_key)
        except Exception:
            # Invalid key, treat as anonymous
            pass
    
    free_tier_service = FreeTierService(redis)
    
    if account:
        # Authenticated: Check credits
        credits_required = PRODUCT_CREDITS["chart2csv"]
        
        if account.balance_usd < COST_PER_EXTRACTION:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail={
                    "code": "INSUFFICIENT_CREDITS",
                    "message": f"Insufficient credits. Required: {credits_required} credits, Available: {usd_to_credits(account.balance_usd)} credits",
                    "balance_credits": usd_to_credits(account.balance_usd),
                    "required_credits": credits_required,
                    "topup_url": "https://kikuai.dev/pricing",
                }
            )
    else:
        # Anonymous: Check free tier with daily + monthly limits
        free_tier_result = await free_tier_service.check_limit("chart2csv", client_ip)
        
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
                }
            )
    
    # Forward to Chart2CSV API
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            # Read file content
            file_content = await file.read()
            
            # Forward request
            api_response = await client.post(
                f"{CHART2CSV_API}/v1/extract",
                files={"file": (file.filename, file_content, file.content_type)},
            )
            
            if api_response.status_code != 200:
                raise HTTPException(
                    status_code=api_response.status_code,
                    detail=api_response.json() if api_response.headers.get("content-type", "").startswith("application/json") else api_response.text
                )
            
            result = api_response.json()
            
    except httpx.RequestError as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "SERVICE_UNAVAILABLE", "message": f"Chart2CSV service unavailable: {str(e)}"}
        )
    
    # Deduct credits / record usage on success
    if account and result.get("success"):
        tracker = UsageTracker(db)
        idempotency_key = f"chart2csv_{account.id}_{secrets.token_hex(8)}"
        
        new_balance = await tracker.track_usage(
            telegram_id=account.telegram_id,
            product_id="chart2csv",
            idempotency_key=idempotency_key,
            units=1,
            metadata={
                "endpoint": "extract",
                "chart_type": result.get("chart_type"),
            }
        )
        
        # Set billing headers
        credits_used = PRODUCT_CREDITS["chart2csv"]
        credits_remaining = usd_to_credits(new_balance)
        response.headers["X-Credits-Used"] = str(credits_used)
        response.headers["X-Credits-Balance"] = str(credits_remaining)
        
        # Add billing info to response
        result["billing"] = {
            "credits_used": credits_used,
            "credits_remaining": credits_remaining,
        }
    elif not account:
        # Record free tier usage
        await free_tier_service.record_usage("chart2csv", client_ip)
        
        # Get updated remaining
        remaining = await free_tier_service.get_remaining("chart2csv", client_ip)
        result["free_tier"] = remaining
    
    return result
