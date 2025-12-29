"""Tests for free_tier_service.py - Daily and monthly limit tracking."""

import pytest
import pytest_asyncio
from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from api.services.free_tier_service import (
    FreeTierService,
    FreeTierLimits,
    FREE_TIER_LIMITS,
)


class TestFreeTierLimits:
    """Tests for free tier limit constants."""
    
    def test_chart2csv_limits(self):
        """Chart2CSV: 3/day, 50/month."""
        limits = FREE_TIER_LIMITS["chart2csv"]
        assert limits.daily == 3
        assert limits.monthly == 50
    
    def test_masker_limits(self):
        """Masker: 100/day, 2000/month."""
        limits = FREE_TIER_LIMITS["masker"]
        assert limits.daily == 100
        assert limits.monthly == 2000
    
    def test_patas_limits(self):
        """PATAS: 100/day, 10000/month."""
        limits = FREE_TIER_LIMITS["patas"]
        assert limits.daily == 100
        assert limits.monthly == 10000
    
    def test_reliapi_limits(self):
        """ReliAPI: 1000/day, 10000/month."""
        limits = FREE_TIER_LIMITS["reliapi"]
        assert limits.daily == 1000
        assert limits.monthly == 10000


class TestFreeTierService:
    """Tests for FreeTierService with mocked Redis."""
    
    @pytest_asyncio.fixture
    async def mock_redis(self):
        """Create mock Redis client."""
        redis = AsyncMock()
        redis.get = AsyncMock(return_value=None)
        redis.incrby = AsyncMock(return_value=1)
        redis.expire = AsyncMock()
        redis.pipeline = MagicMock()
        
        # Pipeline mock
        pipe = AsyncMock()
        pipe.incrby = MagicMock(return_value=pipe)
        pipe.expire = MagicMock(return_value=pipe)
        pipe.execute = AsyncMock(return_value=[1, 1])
        redis.pipeline.return_value = pipe
        
        return redis
    
    @pytest.mark.asyncio
    async def test_check_limit_allowed(self, mock_redis):
        """Should allow when under limit."""
        service = FreeTierService(mock_redis)
        
        result = await service.check_limit("chart2csv", "1.2.3.4")
        
        assert result.allowed is True
        assert result.remaining_daily == 3  # All remaining
        assert result.remaining_monthly == 50
        assert result.limit_daily == 3
        assert result.limit_monthly == 50
    
    @pytest.mark.asyncio
    async def test_check_limit_exceeded_daily(self, mock_redis):
        """Should deny when daily limit exceeded."""
        mock_redis.get = AsyncMock(side_effect=["3", "10"])  # 3 daily, 10 monthly
        service = FreeTierService(mock_redis)
        
        result = await service.check_limit("chart2csv", "1.2.3.4")
        
        assert result.allowed is False
        assert result.remaining_daily == 0
        assert result.remaining_monthly == 40
    
    @pytest.mark.asyncio
    async def test_check_limit_exceeded_monthly(self, mock_redis):
        """Should deny when monthly limit exceeded."""
        mock_redis.get = AsyncMock(side_effect=["2", "50"])  # 2 daily OK, 50 monthly full
        service = FreeTierService(mock_redis)
        
        result = await service.check_limit("chart2csv", "1.2.3.4")
        
        assert result.allowed is False
        assert result.remaining_daily == 1
        assert result.remaining_monthly == 0
    
    @pytest.mark.asyncio
    async def test_record_usage(self, mock_redis):
        """Should increment counters atomically."""
        service = FreeTierService(mock_redis)
        
        daily, monthly = await service.record_usage("masker", "192.168.1.1")
        
        assert daily == 1
        assert monthly == 1
        mock_redis.pipeline.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_get_remaining(self, mock_redis):
        """Should return usage summary."""
        mock_redis.get = AsyncMock(side_effect=["5", "100"])  # 5 daily, 100 monthly
        service = FreeTierService(mock_redis)
        
        remaining = await service.get_remaining("masker", "test-user")
        
        assert remaining["used_today"] == 5
        assert remaining["limit_today"] == 100
        assert remaining["used_month"] == 100
        assert remaining["limit_month"] == 2000
    
    @pytest.mark.asyncio
    async def test_get_all_remaining(self, mock_redis):
        """Should return all products."""
        mock_redis.get = AsyncMock(return_value="0")
        service = FreeTierService(mock_redis)
        
        result = await service.get_all_remaining("test-user")
        
        assert "chart2csv" in result
        assert "masker" in result
        assert "patas" in result
        assert "reliapi" in result
    
    @pytest.mark.asyncio
    async def test_units_parameter(self, mock_redis):
        """Should check multiple units at once."""
        mock_redis.get = AsyncMock(side_effect=["80", "5000"])  # 80 daily, 5000 monthly
        service = FreeTierService(mock_redis)
        
        # Try to use 30 messages (should exceed 100 daily limit)
        result = await service.check_limit("patas", "test-ip", units=30)
        
        # 80 + 30 = 110 > 100 daily limit
        assert result.allowed is False
    
    def test_key_format_daily(self):
        """Daily key should include product, identifier, and date."""
        service = FreeTierService(None)
        key = service._daily_key("chart2csv", "1.2.3.4")
        
        today = date.today().isoformat()
        assert key == f"free:chart2csv:1.2.3.4:daily:{today}"
    
    def test_key_format_monthly(self):
        """Monthly key should include product, identifier, and month."""
        service = FreeTierService(None)
        key = service._monthly_key("masker", "user-123")
        
        month = datetime.utcnow().strftime("%Y-%m")
        assert key == f"free:masker:user-123:monthly:{month}"
