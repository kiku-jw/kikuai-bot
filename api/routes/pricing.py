"""Pricing API - Cost estimator and product pricing list."""

from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from api.services.credits_service import (
    PRODUCT_CREDITS,
    usd_to_credits,
    credits_to_usd,
)
from api.services.free_tier_service import FREE_TIER_LIMITS

router = APIRouter(prefix="/api/v1/pricing", tags=["pricing"])


class ProductPricing(BaseModel):
    """Product pricing info."""
    product_id: str
    name: str
    credits_per_unit: float
    usd_per_unit: float
    unit_name: str
    free_tier_daily: int
    free_tier_monthly: int


class EstimateRequest(BaseModel):
    """Cost estimate request."""
    product_id: str = Field(..., description="Product ID (chart2csv, masker, patas, reliapi)")
    units: int = Field(..., ge=1, description="Number of units to estimate")


class EstimateResponse(BaseModel):
    """Cost estimate response."""
    product_id: str
    units: int
    credits_cost: float
    usd_cost: float
    free_tier_available: Optional[int] = None


# Product metadata
PRODUCT_NAMES = {
    "chart2csv": ("Chart2CSV", "extraction"),
    "masker": ("Masker", "request"),
    "patas": ("PATAS", "100 messages"),
    "reliapi": ("ReliAPI", "request"),
}


@router.get("")
async def list_pricing() -> list[ProductPricing]:
    """
    List all products with their pricing.
    
    Returns Credits and USD pricing per unit plus free tier limits.
    """
    products = []
    
    for product_id, credits in PRODUCT_CREDITS.items():
        name, unit = PRODUCT_NAMES.get(product_id, (product_id, "unit"))
        limits = FREE_TIER_LIMITS.get(product_id)
        
        credits_float = float(credits) if isinstance(credits, Decimal) else credits
        usd = float(credits_to_usd(int(credits * 10))) / 10 if isinstance(credits, Decimal) else float(credits_to_usd(credits))
        
        products.append(ProductPricing(
            product_id=product_id,
            name=name,
            credits_per_unit=credits_float,
            usd_per_unit=usd,
            unit_name=unit,
            free_tier_daily=limits.daily if limits else 0,
            free_tier_monthly=limits.monthly if limits else 0,
        ))
    
    return products


@router.post("/estimate")
async def estimate_cost(request: EstimateRequest) -> EstimateResponse:
    """
    Estimate cost for a batch operation before executing.
    
    Use this to show users how much an operation will cost
    before they commit to it.
    """
    credits = PRODUCT_CREDITS.get(request.product_id)
    if credits is None:
        raise HTTPException(status_code=400, detail=f"Unknown product: {request.product_id}")
    
    # Calculate cost
    credits_float = float(credits) if isinstance(credits, Decimal) else credits
    total_credits = credits_float * request.units
    
    # For PATAS, units are messages, pricing is per 100 messages
    if request.product_id == "patas":
        total_credits = (request.units / 100) * 5  # 5 credits per 100 messages
    
    total_usd = total_credits / 1000  # 1000 credits = $1
    
    # Get free tier info
    limits = FREE_TIER_LIMITS.get(request.product_id)
    free_available = limits.daily if limits else None
    
    return EstimateResponse(
        product_id=request.product_id,
        units=request.units,
        credits_cost=total_credits,
        usd_cost=total_usd,
        free_tier_available=free_available,
    )
