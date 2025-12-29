import json
import redis
from decimal import Decimal, ROUND_HALF_EVEN, getcontext
from datetime import datetime
from typing import Optional, List
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from api.db.base import Account, Transaction, UsageLog, APIKey, Product
from config.settings import REDIS_URL

# Redis client for balance caching
redis_client = redis.from_url(REDIS_URL)

# Set precision context globally for financial calculations
getcontext().prec = 28 # Sufficient for (18, 8) math

# Circuit Breaker state (Global to the service instance)
_redis_cb_state = {"status": "CLOSED", "last_failure": 0.0, "failure_count": 0}
REDIS_CB_THRESHOLD = 5
REDIS_CB_RECOVERY_TIME = 60 # Seconds

class LedgerBalanceService:
    """PostgreSQL-based ledger for financial transactions and usage tracking."""
    
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_account_by_tg_id(self, telegram_id: int, for_update: bool = False) -> Optional[Account]:
        """Get account by Telegram ID. Creates if not exists (Legacy support)."""
        stmt = select(Account).where(Account.telegram_id == telegram_id)
        if for_update:
            stmt = stmt.with_for_update()
            
        result = await self.session.execute(stmt)
        account = result.scalar_one_or_none()
        
        if not account:
            account = Account(telegram_id=telegram_id, balance_usd=Decimal("0.00000000"))
            self.session.add(account)
            await self.session.flush()
            # Cache initial balance in Redis
            self._cache_balance(telegram_id, account.balance_usd)
            
        return account

    async def add_funds(
        self, 
        telegram_id: int, 
        amount: Decimal, 
        idempotency_key: str, 
        description: str = "Top up"
    ) -> Decimal:
        """Add funds to account via a transaction ledger entry."""
        account = await self.get_account_by_tg_id(telegram_id, for_update=True)
        
        # Ensure input amount is Decimal
        amount_dec = Decimal(str(amount))
        
        # Create transaction record
        tx = Transaction(
            account_id=account.id,
            amount_usd=amount_dec,
            type="topup",
            idempotency_key=idempotency_key,
            description=description
        )
        self.session.add(tx)
        
        # Update account balance
        account.balance_usd += amount_dec
        
        try:
            await self.session.commit()
            # Update Redis cache
            self._cache_balance(telegram_id, account.balance_usd)
            return account.balance_usd
        except IntegrityError:
            await self.session.rollback()
            raise ValueError(f"Transaction {idempotency_key} already processed.")

    async def record_usage(
        self,
        telegram_id: int,
        product_id: str,
        units: int,
        cost: Decimal,
        idempotency_key: str,
        metadata: Optional[dict] = None
    ) -> Decimal:
        """
        Record product usage and deduct cost from balance with strict idempotency.
        Enforces 8-decimal precision and Banker's Rounding.
        Uses row-level locking (FOR UPDATE) to prevent race conditions.
        """
        # 1. First check if this specific usage event was already processed (Postgres-level idempotency)
        stmt_tx = select(Transaction).where(Transaction.idempotency_key == idempotency_key)
        tx_result = await self.session.execute(stmt_tx)
        if tx_result.scalar_one_or_none():
            account = await self.get_account_by_tg_id(telegram_id)
            return account.balance_usd if account else Decimal("0.00000000")

        # 2. Lock account row for update
        account = await self.get_account_by_tg_id(telegram_id, for_update=True)
        if not account:
            raise ValueError("Account not found", "ACCOUNT_NOT_FOUND")

        # 3. Enforce Banker's Rounding to 8 decimal places
        cost_dec = Decimal(str(cost)).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_EVEN)
        
        if account.balance_usd < cost_dec:
            # Check for auto-recharge before failing
            if account.auto_recharge_threshold and account.auto_recharge_amount:
                # This would typically trigger a background payment process or notification
                # For Phase 4, we mark it in metadata but still stop if balance < cost
                pass
            raise ValueError("Insufficient balance (Prepaid Hard-Stop)", "BALANCE_EXHAUSTED")

        # 4. Record usage log
        usage = UsageLog(
            account_id=account.id,
            product_id=product_id,
            units_consumed=units,
            cost_usd=cost_dec,
            metadata_json=metadata
        )
        self.session.add(usage)
        
        # 5. Deduct from balance
        account.balance_usd -= cost_dec
        
        # 6. Record as a transaction for the ledger
        tx = Transaction(
            account_id=account.id,
            amount_usd=-cost_dec,
            type="usage",
            product_id=product_id,
            idempotency_key=idempotency_key,
            description=f"Usage: {product_id} ({units} units)"
        )
        self.session.add(tx)
        
        try:
            await self.session.commit()
            
            # 7. Auto-recharge check (Post-usage)
            if account.auto_recharge_threshold and account.balance_usd <= account.auto_recharge_threshold:
                # Trigger internal event (handled by bot/scheduler later)
                # For now, we just log it in AuditLog
                from api.services.account_service import AccountService
                svc = AccountService(self.session)
                await svc.record_audit(
                    account.id, 
                    "AUTO_RECHARGE_TRIGGERED", 
                    metadata={"balance": str(account.balance_usd), "threshold": str(account.auto_recharge_threshold)}
                )

            # 8. Update Redis cache with Circuit Breaker
            if self._is_redis_open():
                try:
                    self._cache_balance(telegram_id, account.balance_usd)
                    self._redis_success()
                except Exception as e:
                    self._redis_failure(e)
            
            return account.balance_usd
        except IntegrityError:
            await self.session.rollback()
            account = await self.get_account_by_tg_id(telegram_id)
            return account.balance_usd if account else Decimal("0.00000000")

    def _is_redis_open(self) -> bool:
        """Check if Circuit Breaker allows Redis calls."""
        if _redis_cb_state["status"] == "OPEN":
            if (datetime.utcnow().timestamp() - _redis_cb_state["last_failure"]) > REDIS_CB_RECOVERY_TIME:
                _redis_cb_state["status"] = "HALF_OPEN"
                return True
            return False
        return True

    def _redis_failure(self, e):
        """Record Redis failure and potentially trip the breaker."""
        _redis_cb_state["failure_count"] += 1
        _redis_cb_state["last_failure"] = datetime.utcnow().timestamp()
        if _redis_cb_state["failure_count"] >= REDIS_CB_THRESHOLD:
            _redis_cb_state["status"] = "OPEN"
            print(f"CRITICAL: Redis Circuit Breaker TRIP! Status: OPEN. Error: {e}")

    def _redis_success(self):
        """Record Redis success and reset breaker."""
        _redis_cb_state["failure_count"] = 0
        _redis_cb_state["status"] = "CLOSED"

    def _cache_balance(self, telegram_id: int, balance: Decimal):
        """Update the high-speed Redis cache for real-time checks."""
        try:
            redis_client.set(f"balance:{telegram_id}", str(balance), ex=3600)
        except Exception as e:
            self._redis_failure(e)

    async def get_cached_balance(self, telegram_id: int) -> Decimal:
        """Get balance from Redis with fallback to Postgres (Degraded Mode)."""
        if self._is_redis_open():
            try:
                val = redis_client.get(f"balance:{telegram_id}")
                if val is not None:
                    self._redis_success()
                    return Decimal(val.decode() if isinstance(val, bytes) else val)
            except Exception as e:
                self._redis_failure(e)
            
        # Fallback to DB
        account = await self.get_account_by_tg_id(telegram_id)
        if account:
            if self._is_redis_open():
                self._cache_balance(telegram_id, account.balance_usd)
            return account.balance_usd
        return Decimal("0.00000000")
