"""Proxy endpoints for ReliAPI."""

from decimal import Decimal
from typing import Dict, Any, Optional
import secrets
from fastapi import APIRouter, HTTPException, Header, Body, status
from pydantic import BaseModel

from api.middleware.auth import verify_api_key, get_user
from api.services.reliapi import ReliAPIService
from api.services.usage_tracker import UsageTracker
from api.services.payment_engine import PaymentEngine, InsufficientBalanceError

router = APIRouter(prefix="/api/v1/proxy", tags=["proxy"])

reliapi_service = ReliAPIService()


class LLMRequest(BaseModel):
    """LLM proxy request model."""
    target: str
    model: str
    messages: list
    cache: int = 3600
    idempotency_key: str = None
    max_retries: int = 3


class HTTPRequest(BaseModel):
    """HTTP proxy request model."""
    url: str
    method: str = "GET"
    headers: Dict[str, str] = {}
    body: Any = None
    cache: int = 3600
    idempotency_key: str = None
    max_retries: int = 3


from api.middleware.auth import require_scope
from api.services.usage_tracker_v2 import UsageTracker
from api.db.base import get_db, Account
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

# Use v2 UsageTracker
usage_tracker_v2 = True # Flag/Marker

@router.post("/llm")
async def proxy_llm(
    request: LLMRequest = Body(...),
    account: Account = Depends(require_scope("reliapi:llm")),
    db: AsyncSession = Depends(get_db)
):
    """Proxy LLM request to ReliAPI with atomic charging."""
    from fastapi import Response
    from api.services.credits_service import usd_to_credits
    
    tracker = UsageTracker(db)
    
    # Use request's idempotency key or generate one
    idempotency_key = request.idempotency_key or f"llm_{account.id}_{secrets.token_hex(8)}"
    
    try:
        # 1. Proxy request to ReliAPI
        reliapi_key = secrets.token_urlsafe(32) # Placeholder for internal routing
        
        result = await reliapi_service.proxy_llm_request(
            api_key=reliapi_key,
            request_data=request.dict(),
        )
        
        # 2. Extract actual cost (default if missing)
        actual_cost_usd = Decimal(str(result.get("meta", {}).get("cost_usd", "0.001")))
        
        # 3. ATOMIC CHARGE & RECORD in Ledger
        new_balance = await tracker.track_usage(
            telegram_id=account.telegram_id,
            product_id="reliapi",
            idempotency_key=idempotency_key,
            units=1,
            metadata={
                "endpoint": "proxy/llm",
                "model": request.model,
                "target": request.target,
                "actual_cost": float(actual_cost_usd)
            }
        )
        
        # Add billing info
        result["billing"] = {
            "credits_used": 0.1,  # Base 0.1 credit per request
            "credits_remaining": usd_to_credits(new_balance),
        }
        
        return result
    
    except ValueError as e:
        # Handle "Insufficient balance" or "Already processed"
        raise HTTPException(status_code=402, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Proxy error: {str(e)}")


@router.post("/http")
async def proxy_http(
    request: HTTPRequest = Body(...),
    account: Account = Depends(require_scope("reliapi:http")),
    db: AsyncSession = Depends(get_db)
):
    """Proxy HTTP request to ReliAPI with atomic charging."""
    tracker = UsageTracker(db)
    idempotency_key = request.idempotency_key or f"http_{account.id}_{secrets.token_hex(8)}"
    
    try:
        # 1. Proxy request to ReliAPI
        reliapi_key = secrets.token_urlsafe(32)
        
        result = await reliapi_service.proxy_http_request(
            api_key=reliapi_key,
            request_data=request.dict(),
        )
        
        # 2. Extract actual cost
        actual_cost_usd = Decimal(str(result.get("meta", {}).get("cost_usd", "0.0005")))
        
        # 3. ATOMIC CHARGE & RECORD
        await tracker.track_usage(
            telegram_id=account.telegram_id,
            product_id="reliapi",
            idempotency_key=idempotency_key,
            units=1,
            metadata={
                "endpoint": "proxy/http",
                "method": request.method,
                "url": request.url,
                "actual_cost": float(actual_cost_usd)
            }
        )
        
        return result
    
    except ValueError as e:
        raise HTTPException(status_code=402, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Proxy error: {str(e)}")

