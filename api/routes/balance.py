"""Balance and usage endpoints with Credits display."""

from decimal import Decimal
from typing import Optional
from fastapi import APIRouter, HTTPException, Header, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime

from api.db.base import get_db, Account
from api.services.account_service import AccountService
from api.services.credits_service import usd_to_credits, format_credits
from api.services.free_tier_service import FreeTierService
from api.services.usage_tracker_v2 import UsageTracker
from api.dependencies import get_redis

router = APIRouter(prefix="/api/v1", tags=["balance"])


class BalanceResponse(BaseModel):
    """Balance response model with Credits."""
    balance_usd: float
    balance_credits: int
    currency: str = "USD"
    free_tier: Optional[dict] = None


class UsageResponse(BaseModel):
    """Usage response model."""
    period: str
    balance_usd: float
    balance_credits: int
    usage: list


class HistoryItem(BaseModel):
    """Transaction history item."""
    id: str
    type: str
    amount_usd: float
    amount_credits: int
    description: Optional[str]
    created_at: str


@router.get("/balance", response_model=BalanceResponse)
async def get_balance(
    x_api_key: str = Header(..., alias="X-API-Key"),
    db: AsyncSession = Depends(get_db),
    redis = Depends(get_redis),
):
    """
    Get current balance in USD and Credits.
    
    Also returns free tier usage for anonymous/unauthenticated context.
    """
    account_service = AccountService(db)
    
    try:
        account, _ = await account_service.verify_key(x_api_key)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid API key")
    
    balance_usd = float(account.balance_usd)
    balance_credits = usd_to_credits(account.balance_usd)
    
    # Get free tier status
    free_tier_service = FreeTierService(redis)
    identifier = str(account.id)
    free_tier = await free_tier_service.get_all_remaining(identifier)
    
    return BalanceResponse(
        balance_usd=balance_usd,
        balance_credits=balance_credits,
        free_tier=free_tier,
    )


@router.get("/usage", response_model=UsageResponse)
async def get_usage(
    month: Optional[str] = None,
    x_api_key: str = Header(..., alias="X-API-Key"),
    db: AsyncSession = Depends(get_db),
):
    """Get usage statistics for current or specified month."""
    account_service = AccountService(db)
    
    try:
        account, _ = await account_service.verify_key(x_api_key)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid API key")
    
    tracker = UsageTracker(db)
    stats = await tracker.get_usage_stats(account.telegram_id, month)
    
    # Add credits info
    balance_usd = float(stats.get("balance_usd", 0))
    
    return UsageResponse(
        period=stats.get("period", "current_month"),
        balance_usd=balance_usd,
        balance_credits=usd_to_credits(Decimal(str(balance_usd))),
        usage=stats.get("usage", []),
    )


@router.get("/history")
async def get_history(
    limit: int = 20,
    x_api_key: str = Header(..., alias="X-API-Key"),
    db: AsyncSession = Depends(get_db),
):
    """Get transaction history."""
    from sqlalchemy import select
    from api.db.base import Transaction
    
    account_service = AccountService(db)
    
    try:
        account, _ = await account_service.verify_key(x_api_key)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid API key")
    
    # Get transactions
    stmt = select(Transaction).where(
        Transaction.account_id == account.id
    ).order_by(Transaction.created_at.desc()).limit(limit)
    
    result = await db.execute(stmt)
    transactions = result.scalars().all()
    
    return [
        {
            "id": str(tx.id),
            "type": tx.type,
            "amount_usd": float(tx.amount_usd),
            "amount_credits": usd_to_credits(tx.amount_usd),
            "description": tx.description,
            "created_at": tx.created_at.isoformat(),
        }
        for tx in transactions
    ]
