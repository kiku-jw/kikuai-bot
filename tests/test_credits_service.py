"""Tests for credits_service.py - USD ↔ Credits conversion."""

import pytest
from decimal import Decimal

from api.services.credits_service import (
    usd_to_credits,
    credits_to_usd,
    format_credits,
    format_credits_cost,
    PRODUCT_CREDITS,
    get_product_credits,
)


class TestUsdToCredits:
    """Tests for USD to Credits conversion."""
    
    def test_basic_conversion(self):
        """1000 credits = $1 USD."""
        assert usd_to_credits(Decimal("1.00")) == 1000
        assert usd_to_credits(Decimal("5.00")) == 5000
        assert usd_to_credits(Decimal("10.00")) == 10000
    
    def test_small_amounts(self):
        """Fractional USD amounts."""
        assert usd_to_credits(Decimal("0.05")) == 50  # Chart2CSV price
        assert usd_to_credits(Decimal("0.001")) == 1  # Masker price
        assert usd_to_credits(Decimal("0.005")) == 5  # PATAS price
    
    def test_zero(self):
        """Zero USD = zero credits."""
        assert usd_to_credits(Decimal("0.00")) == 0
    
    def test_negative_raises_error(self):
        """Negative amounts should raise ValueError."""
        with pytest.raises(ValueError, match="cannot be negative"):
            usd_to_credits(Decimal("-1.00"))
    
    def test_rounding(self):
        """Rounding to nearest integer (banker's rounding)."""
        # $0.0005 = 0.5 credits → rounds to 0 (banker's rounding)
        assert usd_to_credits(Decimal("0.0005")) == 0
        # $0.0015 = 1.5 credits → rounds to 2 (banker's rounding)
        assert usd_to_credits(Decimal("0.0015")) == 2


class TestCreditsToUsd:
    """Tests for Credits to USD conversion."""
    
    def test_basic_conversion(self):
        """1000 credits = $1 USD."""
        assert credits_to_usd(1000) == Decimal("1.00000000")
        assert credits_to_usd(5000) == Decimal("5.00000000")
    
    def test_small_amounts(self):
        """Small credit amounts."""
        assert credits_to_usd(50) == Decimal("0.05000000")  # Chart2CSV
        assert credits_to_usd(1) == Decimal("0.00100000")   # Masker
        assert credits_to_usd(5) == Decimal("0.00500000")   # PATAS
    
    def test_zero(self):
        """Zero credits = zero USD."""
        assert credits_to_usd(0) == Decimal("0.00000000")
    
    def test_negative_raises_error(self):
        """Negative credits should raise ValueError."""
        with pytest.raises(ValueError, match="cannot be negative"):
            credits_to_usd(-100)


class TestFormatCredits:
    """Tests for credits formatting."""
    
    def test_format_basic(self):
        """Format with comma separators."""
        assert format_credits(Decimal("5.00")) == "5,000 credits"
        assert format_credits(Decimal("1.00")) == "1,000 credits"
    
    def test_format_singular(self):
        """Singular 'credit' for 1."""
        assert format_credits(Decimal("0.001")) == "1 credit"
    
    def test_format_large(self):
        """Large amounts with comma separators."""
        assert format_credits(Decimal("100.00")) == "100,000 credits"


class TestProductCredits:
    """Tests for product pricing."""
    
    def test_chart2csv_price(self):
        """Chart2CSV = 50 credits."""
        assert get_product_credits("chart2csv") == 50
    
    def test_masker_price(self):
        """Masker = 1 credit."""
        assert get_product_credits("masker") == 1
    
    def test_patas_price(self):
        """PATAS = 5 credits per 100 messages."""
        assert get_product_credits("patas") == 5
    
    def test_reliapi_price(self):
        """ReliAPI = 0.1 credits."""
        assert get_product_credits("reliapi") == Decimal("0.1")
    
    def test_unknown_product(self):
        """Unknown product returns 0."""
        assert get_product_credits("unknown") == 0


class TestRoundTrip:
    """Tests for round-trip conversion consistency."""
    
    def test_usd_credits_usd(self):
        """USD → Credits → USD should be consistent."""
        original = Decimal("10.00")
        credits = usd_to_credits(original)
        back_to_usd = credits_to_usd(credits)
        assert back_to_usd == original
    
    def test_typical_balances(self):
        """Test with typical user balances."""
        for usd in [Decimal("5.00"), Decimal("25.00"), Decimal("100.00")]:
            credits = usd_to_credits(usd)
            back = credits_to_usd(credits)
            assert back == usd
