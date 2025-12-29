from typing import Optional, List
from datetime import datetime
from fastapi import APIRouter, HTTPException, Header, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc

from api.db.base import get_db, Account, Transaction as TransactionModel, UsageLog
from api.services.account_service import AccountService
from api.services.usage_tracker_v2 import UsageTracker

router = APIRouter(prefix="/api/v1", tags=["balance"])

class BalanceResponse(BaseModel):
    balance_usd: float
    currency: str = "USD"

class UsageStat(BaseModel):
    product_id: str
    units: int
    cost_usd: float

class UsageResponse(BaseModel):
    telegram_id: int
    period: str
    balance_usd: float
    usage: List[UsageStat]

class TransactionResponse(BaseModel):
    id: str
    amount_usd: float
    type: str
    description: Optional[str]
    created_at: str

from api.middleware.auth import get_current_account

@router.get("/balance", response_model=BalanceResponse)
async def get_balance(
    account: Account = Depends(get_current_account)
):
    """Get current balance from PostgreSQL (Source of Truth)."""
    return BalanceResponse(balance_usd=float(account.balance_usd))

@router.get("/usage", response_model=UsageResponse)
async def get_usage(
    month: Optional[str] = None,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db)
):
    """Get summarized usage statistics from PostgreSQL ledger."""
    tracker = UsageTracker(db)
    stats = await tracker.get_usage_stats(account.telegram_id, month)
    
    return UsageResponse(**stats)

@router.get("/history", response_model=List[TransactionResponse])
async def get_history(
    limit: int = 20,
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db)
):
    """Get real transaction history from the PostgreSQL Ledger."""

    stmt = (
        select(TransactionModel)
        .where(TransactionModel.account_id == account.id)
        .order_by(desc(TransactionModel.created_at))
        .limit(limit)
    )
    result = await db.execute(stmt)
    transactions = result.scalars().all()
    
    return [
        TransactionResponse(
            id=str(tx.id),
            amount_usd=float(tx.amount_usd),
            type=tx.type,
            description=tx.description,
            created_at=tx.created_at.isoformat()
        )
        for tx in transactions
    ]


@router.get("/usage/summary")
async def get_usage_summary(
    account: Account = Depends(get_current_account),
    db: AsyncSession = Depends(get_db)
):
    """
    Get simplified usage summary for dashboard.
    Returns: {chart2csv: 123, patas: 45, masker: 78, reliapi: 12}
    """
    tracker = UsageTracker(db)
    stats = await tracker.get_usage_stats(account.telegram_id)
    
    # Convert list of usage stats to simple dict
    result = {}
    for item in stats.get("usage", []):
        result[item["product_id"]] = item["units"]
    
    # Ensure all products are present (with 0 if no usage)
    for product_id in ["chart2csv", "patas", "masker", "reliapi"]:
        if product_id not in result:
            result[product_id] = 0
    
    return result

