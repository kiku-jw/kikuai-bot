"""
Credits Service - USD ↔ Credits conversion layer.

1 credit = $0.001 USD (1000 credits = $1)
Internally everything stays USD, this is just for display/API.
"""

from decimal import Decimal, ROUND_HALF_EVEN

# Conversion rate: 1000 credits = $1 USD
CREDITS_PER_USD = 1000


def usd_to_credits(usd: Decimal) -> int:
    """
    Convert USD amount to credits.
    
    Examples:
        $5.00 → 5,000 credits
        $0.05 → 50 credits
        $0.001 → 1 credit
    """
    if usd < 0:
        raise ValueError("USD amount cannot be negative")
    
    credits = usd * CREDITS_PER_USD
    return int(credits.quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))


def credits_to_usd(credits: int) -> Decimal:
    """
    Convert credits to USD amount.
    
    Examples:
        5,000 credits → $5.00
        50 credits → $0.05
        1 credit → $0.001
    """
    if credits < 0:
        raise ValueError("Credits cannot be negative")
    
    usd = Decimal(credits) / CREDITS_PER_USD
    return usd.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_EVEN)


def format_credits(usd: Decimal) -> str:
    """
    Format USD amount as credits string for display.
    
    Examples:
        $5.00 → "5,000 credits"
        $0.05 → "50 credits"
        $0.001 → "1 credit"
    """
    credits = usd_to_credits(usd)
    formatted = f"{credits:,}"
    return f"{formatted} {'credit' if credits == 1 else 'credits'}"


def format_credits_cost(credits: int) -> str:
    """
    Format credits cost for display.
    
    Examples:
        50 → "50 credits"
        1 → "1 credit"
        0.1 → "0.1 credits" (for fractional like ReliAPI)
    """
    if isinstance(credits, float) or (isinstance(credits, Decimal) and credits != int(credits)):
        return f"{credits} credits"
    return f"{credits:,} {'credit' if credits == 1 else 'credits'}"


# Product pricing in credits (matching spec)
PRODUCT_CREDITS = {
    "chart2csv": 50,      # $0.05 per extraction
    "masker": 1,          # $0.001 per request
    "patas": 5,           # $0.005 per 100 messages (0.05 per message)
    "reliapi": Decimal("0.1"),  # $0.0001 per request
}


def get_product_credits(product_id: str) -> int | Decimal:
    """Get credits cost for a product."""
    return PRODUCT_CREDITS.get(product_id, 0)
