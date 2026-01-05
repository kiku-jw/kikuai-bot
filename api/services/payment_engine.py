"""
Payment Engine - Abstract payment processing for KikuAI Bot.

Supports multiple payment providers (Paddle, Telegram Stars) with
unified interface, transaction management, and error handling.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
import asyncio
import hashlib
import json
import logging
import secrets
import time
from typing import Optional, Any, List, Dict, Union

import httpx

logger = logging.getLogger(__name__)


# ============================================================================
# Enums
# ============================================================================

class PaymentMethod(str, Enum):
    PADDLE = "paddle"
    TELEGRAM_STARS = "telegram_stars"
    LEMONSQUEEZY = "lemonsqueezy"
    CREEM = "creem"


class PaymentStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    REFUNDED = "refunded"
    CANCELLED = "cancelled"


class TransactionType(str, Enum):
    TOPUP = "topup"
    USAGE = "usage"
    REFUND = "refund"
    ADJUSTMENT = "adjustment"


# ============================================================================
# Data Models
# ============================================================================

@dataclass
class PaymentRequest:
    """Request to create a payment."""
    user_id: int
    amount_usd: Decimal
    method: PaymentMethod
    idempotency_key: str = field(default_factory=lambda: secrets.token_hex(16))
    metadata: dict = field(default_factory=dict)
    
    def __post_init__(self):
        if self.amount_usd <= 0:
            raise ValueError("Amount must be positive")


@dataclass
class PaymentResult:
    """Result of payment creation."""
    payment_id: str
    status: PaymentStatus
    checkout_url: Optional[str] = None
    invoice_link: Optional[str] = None
    expires_at: Optional[datetime] = None
    error: Optional[str] = None
    metadata: dict = field(default_factory=dict)
    
    @property
    def is_success(self) -> bool:
        return self.status != PaymentStatus.FAILED


@dataclass
class Transaction:
    """Record of a financial transaction."""
    id: str
    user_id: int
    type: TransactionType
    amount_usd: Decimal
    balance_before: Decimal
    balance_after: Decimal
    source: str
    external_id: Optional[str] = None
    metadata: dict = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)
    
    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "type": self.type.value,
            "amount_usd": str(self.amount_usd),
            "balance_before": str(self.balance_before),
            "balance_after": str(self.balance_after),
            "source": self.source,
            "external_id": self.external_id,
            "metadata": self.metadata,
            "created_at": self.created_at.isoformat(),
        }


@dataclass
class WebhookEvent:
    """Incoming webhook event from payment provider."""
    provider: PaymentMethod
    event_type: str
    event_id: str
    data: dict
    raw_body: bytes
    signature: str
    timestamp: datetime = field(default_factory=datetime.utcnow)


# ============================================================================
# Exceptions
# ============================================================================

class PaymentError(Exception):
    """Base exception for payment errors."""
    pass


class InsufficientBalanceError(PaymentError):
    """User doesn't have enough balance."""
    def __init__(self, current: Decimal, required: Decimal):
        self.current = current
        self.required = required
        super().__init__(f"Insufficient balance: have {current}, need {required}")


class InvalidSignatureError(PaymentError):
    """Webhook signature verification failed."""
    pass


class DuplicatePaymentError(PaymentError):
    """Payment with this idempotency key already processed."""
    def __init__(self, existing_id: str):
        self.existing_id = existing_id
        super().__init__(f"Duplicate payment: {existing_id}")


class ProviderError(PaymentError):
    """Error from payment provider."""
    def __init__(self, provider: str, code: str, message: str):
        self.provider = provider
        self.code = code
        super().__init__(f"{provider} error [{code}]: {message}")


# ============================================================================
# Abstract Interfaces
# ============================================================================

class PaymentProvider(ABC):
    """Abstract interface for payment providers."""
    
    @property
    @abstractmethod
    def name(self) -> str:
        """Provider name (e.g., 'paddle', 'telegram_stars')."""
        pass
    
    @abstractmethod
    async def create_checkout(
        self,
        request: PaymentRequest,
        success_url: str,
        cancel_url: str,
    ) -> PaymentResult:
        """Create a checkout session."""
        pass
    
    @abstractmethod
    async def verify_webhook(self, event: WebhookEvent) -> bool:
        """Verify webhook signature."""
        pass
    
    @abstractmethod
    async def process_webhook(self, event: WebhookEvent) -> Optional[Transaction]:
        """Process webhook and return transaction if applicable."""
        pass
    
    @abstractmethod
    async def get_payment_status(self, payment_id: str) -> PaymentStatus:
        """Get current payment status."""
        pass
    
    async def refund(self, payment_id: str, amount: Optional[Decimal] = None) -> bool:
        """Refund a payment. Override if supported."""
        raise NotImplementedError(f"{self.name} does not support refunds")


class BalanceManager(ABC):
    """Abstract interface for balance operations."""
    
    @abstractmethod
    async def get_balance(self, user_id: int) -> Decimal:
        """Get user's current balance."""
        pass
    
    @abstractmethod
    async def update_balance(
        self,
        user_id: int,
        amount: Decimal,
        transaction: Transaction,
        idempotency_key: str,
    ) -> Decimal:
        """
        Atomically update balance and record transaction.
        Returns new balance.
        """
        pass
    
    @abstractmethod
    async def check_idempotency(self, key: str) -> Optional[dict]:
        """Check if operation was already performed."""
        pass


class NotificationService(ABC):
    """Abstract interface for user notifications."""
    
    @abstractmethod
    async def notify_payment_success(
        self,
        user_id: int,
        amount: Decimal,
        new_balance: Decimal,
    ) -> None:
        pass
    
    @abstractmethod
    async def notify_payment_failed(
        self,
        user_id: int,
        reason: str,
    ) -> None:
        pass
    
    @abstractmethod
    async def notify_low_balance(
        self,
        user_id: int,
        current_balance: Decimal,
    ) -> None:
        pass


# ============================================================================
# Payment Engine
# ============================================================================

class PaymentEngine:
    """
    Main payment processing engine.
    
    Orchestrates payment providers, balance management, and notifications.
    """
    
    def __init__(
        self,
        balance_manager: BalanceManager,
        notification_service: NotificationService,
        low_balance_threshold: Decimal = Decimal("5.00"),
    ):
        self.balance_manager = balance_manager
        self.notifications = notification_service
        self.low_balance_threshold = low_balance_threshold
        self._providers: dict[PaymentMethod, PaymentProvider] = {}
    
    def register_provider(self, method: PaymentMethod, provider: PaymentProvider):
        """Register a payment provider."""
        self._providers[method] = provider
    
    def get_provider(self, method: PaymentMethod) -> PaymentProvider:
        """Get provider by payment method."""
        if method not in self._providers:
            raise PaymentError(f"Provider not registered: {method}")
        return self._providers[method]
    
    async def create_payment(
        self,
        request: PaymentRequest,
        success_url: str,
        cancel_url: str,
    ) -> PaymentResult:
        """
        Create a new payment.
        
        1. Check idempotency
        2. Get provider
        3. Create checkout
        4. Return result
        """
        # Check idempotency
        existing = await self.balance_manager.check_idempotency(request.idempotency_key)
        if existing:
            return PaymentResult(
                payment_id=existing.get("payment_id", ""),
                status=PaymentStatus(existing.get("status", "pending")),
            )
        
        # Get provider and create checkout
        provider = self.get_provider(request.method)
        result = await provider.create_checkout(request, success_url, cancel_url)
        
        return result
    
    async def process_webhook(self, event: WebhookEvent) -> Optional[Transaction]:
        """
        Process incoming webhook.
        
        1. Verify signature
        2. Check idempotency
        3. Process payment
        4. Update balance
        5. Send notification
        """
        provider = self.get_provider(event.provider)
        
        # Verify signature
        if not await provider.verify_webhook(event):
            raise InvalidSignatureError(f"Invalid signature from {event.provider}")
        
        # Check idempotency
        existing = await self.balance_manager.check_idempotency(event.event_id)
        if existing:
            return None  # Already processed
        
        # Process through provider
        transaction = await provider.process_webhook(event)
        if not transaction:
            return None
        
        # Update balance
        new_balance = await self.balance_manager.update_balance(
            user_id=transaction.user_id,
            amount=transaction.amount_usd,
            transaction=transaction,
            idempotency_key=event.event_id,
        )
        
        # Send notification
        if transaction.type == TransactionType.TOPUP:
            await self.notifications.notify_payment_success(
                user_id=transaction.user_id,
                amount=transaction.amount_usd,
                new_balance=new_balance,
            )
        elif transaction.type == TransactionType.REFUND:
            # Notify and check for negative balance
            pass
        
        # Check low balance
        if new_balance < self.low_balance_threshold:
            await self.notifications.notify_low_balance(
                user_id=transaction.user_id,
                current_balance=new_balance,
            )
        
        return transaction
    
    async def charge_usage(
        self,
        user_id: int,
        amount: Decimal,
        product_id: str,
        details: dict,
    ) -> Transaction:
        """
        Charge user for API usage.
        
        Raises InsufficientBalanceError if balance too low.
        """
        current_balance = await self.balance_manager.get_balance(user_id)
        
        if current_balance < amount:
            raise InsufficientBalanceError(current_balance, amount)
        
        transaction = Transaction(
            id=f"txn_usage_{secrets.token_hex(8)}",
            user_id=user_id,
            type=TransactionType.USAGE,
            amount_usd=-amount,  # Negative for charges
            balance_before=current_balance,
            balance_after=current_balance - amount,
            source=product_id,
            metadata=details,
        )
        
        idempotency_key = f"usage:{user_id}:{transaction.id}"
        
        await self.balance_manager.update_balance(
            user_id=user_id,
            amount=-amount,
            transaction=transaction,
            idempotency_key=idempotency_key,
        )
        
        return transaction


# ============================================================================
# Provider Implementations (stubs - to be implemented)
# ============================================================================

class PaddleProvider(PaymentProvider):
    """Paddle payment provider implementation."""
    
    # Product ID from Paddle Dashboard
    PRODUCT_ID = "pro_01kbtwsmrfcxsv5fpcyb83v7mn"
    
    # Price IDs from Paddle Dashboard
    PRICE_TIERS = {
        5: "pri_01kbtwwg8hdst8kttvpq4dm2b5",    # $5
        10: "pri_01kbtwwv5tfbq5jb5j24wj5c8h",   # $10
        25: "pri_01kbtwx65x07dxsp5nps3j17r7",   # $25
        50: "pri_01kbtwxf1hmdez5sptk0j4qbjr",   # $50
        100: "pri_01kbtwxq1zjctb4dkg62cpv6ca",   # $100
    }
    
    def __init__(
        self,
        api_key: str,
        webhook_secret: str,
        sandbox: bool = False,
        max_retries: int = 3,
    ):
        self.api_key = api_key
        self.webhook_secret = webhook_secret
        self.sandbox = sandbox
        self.max_retries = max_retries
        self.base_url = (
            "https://sandbox-api.paddle.com" if sandbox
            else "https://api.paddle.com"
        )
        self._client: Optional[Any] = None
    
    @property
    def name(self) -> str:
        return "paddle"
    
    async def _get_client(self):
        """Get or create httpx client."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=30.0,
            )
        return self._client
    
    async def _request_with_retry(
        self,
        method: str,
        path: str,
        **kwargs,
    ) -> dict:
        """Make HTTP request with exponential backoff retry."""
        
        client = await self._get_client()
        
        from api.services.metrics import api_request_duration, track_api_request
        
        last_error: Optional[Exception] = None
        
        for attempt in range(self.max_retries):
            try:
                start_time = time.time()
                response = await client.request(method, path, **kwargs)
                duration = time.time() - start_time
                
                # Track API duration
                api_request_duration.labels(
                    provider="paddle",
                    endpoint=path.split("?")[0]
                ).observe(duration)
                
                # Log response for debugging
                logger.debug(f"Paddle API {method} {path}: {response.status_code}")
                
                if response.status_code == 429:
                    # Rate limited - wait and retry
                    if attempt < self.max_retries - 1:
                        retry_after = int(response.headers.get("Retry-After", 5))
                        await asyncio.sleep(retry_after)
                        continue
                    else:
                        break
                
                if response.status_code >= 500:
                    # Server error - retry with backoff
                    if attempt < self.max_retries - 1:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    else:
                        break
                
                # Parse response
                data = response.json()
                
                if response.status_code >= 400:
                    # Client error - don't retry
                    error = data.get("error", {})
                    raise ProviderError(
                        provider="paddle",
                        code=error.get("code", str(response.status_code)),
                        message=error.get("detail", response.text),
                    )
                
                return data
                
            except (httpx.TimeoutException, httpx.RequestError) as e:
                # Network errors - retry with backoff
                if attempt < self.max_retries - 1:
                    last_error = ProviderError("paddle", "network", str(e))
                    await asyncio.sleep(2 ** attempt)
                else:
                    last_error = ProviderError("paddle", "network", str(e))
                    break
            except ProviderError:
                raise
            except Exception as e:
                last_error = ProviderError("paddle", "unknown", str(e))
                break
        
        if last_error:
            raise last_error
        raise ProviderError("paddle", "max_retries", "Max retries exceeded")
    
    async def create_checkout(
        self,
        request: PaymentRequest,
        success_url: str,
        cancel_url: str,
    ) -> PaymentResult:
        """Create Paddle checkout session."""
        from api.services.metrics import PaymentTimer, track_payment_request, track_payment_error
        
        try:
            with PaymentTimer("paddle"):
                # Determine amount and price
                amount_int = int(request.amount_usd)
                
                # Get price_id from PRICE_TIERS
                price_id = self.PRICE_TIERS.get(amount_int)
                
                if not price_id:
                    logger.error(f"No price_id found for amount ${amount_int}")
                    return PaymentResult(
                        payment_id="",
                        status=PaymentStatus.FAILED,
                        error=f"Amount ${amount_int} not supported. Available: {list(self.PRICE_TIERS.keys())}",
                    )
                
                # Build request payload for Paddle API v2
                # Using price_id from Paddle Dashboard
                payload = {
                    "items": [
                        {
                            "price_id": price_id,
                            "quantity": 1,
                        }
                    ],
                    "custom_data": json.dumps({
                        "user_id": str(request.user_id),
                        "idempotency_key": request.idempotency_key,
                        "amount_usd": str(request.amount_usd),
                    }),
                    "checkout": {
                        "url": success_url,
                    },
                }
                
                # Log payload for debugging
                logger.debug(f"Paddle payload: {json.dumps(payload, indent=2)}")
                
                # Add cancel URL if different from success
                if cancel_url and cancel_url != success_url:
                    # Paddle doesn't have separate cancel URL in v2 API
                    # User can close the checkout to cancel
                    pass
                
                logger.info(f"Creating Paddle checkout for user {request.user_id}, ${request.amount_usd}")
                
                # Create transaction
                response = await self._request_with_retry(
                    "POST",
                    "/transactions",
                    json=payload,
                )
                
                data = response.get("data", {})
                transaction_id = data.get("id")
                checkout_url = data.get("checkout", {}).get("url")
            
            track_payment_request("paddle", "success")
            
            return PaymentResult(
                payment_id=transaction_id,
                status=PaymentStatus.PENDING,
                checkout_url=checkout_url,
                expires_at=None,  # Paddle checkouts don't expire
            )
            
        except ProviderError as e:
            track_payment_request("paddle", "failed")
            track_payment_error("paddle", e.code)
            raise
        except Exception as e:
            track_payment_request("paddle", "failed")
            track_payment_error("paddle", "unknown")
            logger.error(f"Failed to create Paddle checkout: {e}")
            return PaymentResult(
                payment_id="",
                status=PaymentStatus.FAILED,
                error=str(e),
            )
    
    async def verify_webhook(self, event: WebhookEvent) -> bool:
        """Verify Paddle webhook signature (HMAC-SHA256)."""
        import hmac
        import time
        import logging
        
        logger = logging.getLogger(__name__)
        
        try:
            # Parse signature: ts=xxx;h1=xxx
            parts = {}
            for item in event.signature.split(";"):
                if "=" in item:
                    k, v = item.split("=", 1)
                    parts[k] = v
            
            timestamp = parts.get("ts")
            received = parts.get("h1")
            
            if not timestamp or not received:
                logger.warning("Missing timestamp or signature in Paddle webhook")
                return False
            
            # Check timestamp age (max 5 minutes)
            age = abs(int(timestamp) - int(time.time()))
            if age > 300:
                logger.warning(f"Paddle webhook too old: {age}s")
                return False
            
            # Compute expected signature
            signed_payload = f"{timestamp}:{event.raw_body.decode()}"
            expected = hmac.new(
                self.webhook_secret.encode(),
                signed_payload.encode(),
                hashlib.sha256,
            ).hexdigest()
            
            if not hmac.compare_digest(expected, received):
                logger.warning("Paddle webhook signature mismatch")
                return False
            
            return True
            
        except Exception as e:
            logger.error(f"Error verifying Paddle webhook: {e}")
            return False
    
    async def process_webhook(self, event: WebhookEvent) -> Optional[Transaction]:
        """Process Paddle webhook event."""
        from api.services.metrics import WebhookTimer, track_webhook_event
        
        event_type = event.event_type
        data = event.data
        
        with WebhookTimer("paddle"):
            logger.info(f"Processing Paddle webhook: {event_type}")
        
        from api.services.metrics import track_webhook_event
        
        if event_type == "transaction.completed":
            result = await self._handle_transaction_completed(data)
            track_webhook_event("paddle", event_type, "success" if result else "ignored")
            return result
        
        elif event_type == "transaction.payment_failed":
            await self._handle_payment_failed(data)
            track_webhook_event("paddle", event_type, "processed")
            return None
        
        elif event_type == "transaction.refunded":
            result = await self._handle_refund(data)
            track_webhook_event("paddle", event_type, "success" if result else "ignored")
            return result
        
        else:
            logger.debug(f"Ignoring Paddle event type: {event_type}")
            track_webhook_event("paddle", event_type, "ignored")
            return None
    
    async def _handle_transaction_completed(self, data: dict) -> Optional[Transaction]:
        """Handle successful payment."""
        import logging
        import json
        
        logger = logging.getLogger(__name__)
        
        try:
            transaction_id = data.get("id")
            custom_data_raw = data.get("custom_data", {})
            
            # Parse custom data - Paddle may send as object or JSON string
            if isinstance(custom_data_raw, str):
                try:
                    custom_data = json.loads(custom_data_raw)
                except json.JSONDecodeError:
                    custom_data = {}
            elif isinstance(custom_data_raw, dict):
                custom_data = custom_data_raw
            else:
                custom_data = {}
            
            # Get user_id - frontend sends as "user_id" with telegram_id value
            user_id_str = custom_data.get("user_id", "0")
            user_id = int(user_id_str) if user_id_str else 0
            
            if not user_id:
                logger.error(f"Missing user_id in Paddle transaction {transaction_id}, custom_data: {custom_data}")
                return None
            
            # Get actual amount from Paddle
            details = data.get("details", {})
            totals = details.get("totals", {})
            paddle_amount = totals.get("total", "0")
            
            # Convert from cents to dollars
            amount_usd = Decimal(paddle_amount) / 100
            
            # Validate amount matches expected (if provided)
            expected_amount_str = custom_data.get("amount_usd", "")
            if expected_amount_str:
                expected_amount = Decimal(expected_amount_str)
                if abs(amount_usd - expected_amount) > Decimal("0.10"):
                    logger.warning(
                        f"Amount mismatch in Paddle transaction {transaction_id}: "
                        f"expected ${expected_amount}, got ${amount_usd}"
                    )
            
            logger.info(f"Paddle payment success: user={user_id}, amount=${amount_usd}")
            
            # Create transaction
            return Transaction(
                id=f"txn_paddle_{secrets.token_hex(8)}",
                user_id=user_id,
                type=TransactionType.TOPUP,
                amount_usd=amount_usd,
                balance_before=Decimal("0"),  # Will be set by BalanceManager
                balance_after=Decimal("0"),   # Will be set by BalanceManager
                source="paddle",
                external_id=transaction_id,
                metadata={
                    "paddle_transaction_id": transaction_id,
                    "custom_data": custom_data,
                },
            )
            
        except Exception as e:
            logger.error(f"Error processing Paddle payment: {e}")
            return None
    
    async def _handle_payment_failed(self, data: dict):
        """Handle failed payment - log and notify."""
        import logging
        import json
        
        logger = logging.getLogger(__name__)
        
        transaction_id = data.get("id")
        error_code = data.get("error", {}).get("code", "unknown")
        
        custom_data_str = data.get("custom_data", "{}")
        try:
            custom_data = json.loads(custom_data_str)
        except json.JSONDecodeError:
            custom_data = {}

        user_id = custom_data.get("user_id")

        logger.warning(
            f"Paddle payment failed: transaction={transaction_id}, "
            f"user={user_id}, error={error_code}"
        )
        
        # Note: Notification will be sent by PaymentEngine or caller
    
    async def _handle_refund(self, data: dict) -> Optional[Transaction]:
        """Handle refund - deduct from balance."""
        import logging
        import json
        
        logger = logging.getLogger(__name__)
        
        try:
            transaction_id = data.get("id")
            custom_data_str = data.get("custom_data", "{}")
            
            try:
                custom_data = json.loads(custom_data_str)
            except json.JSONDecodeError:
                custom_data = {}

            user_id = int(custom_data.get("user_id", 0))
            
            if not user_id:
                logger.error(f"Missing user_id in Paddle refund {transaction_id}")
                return None
            
            # Get refund amount
            details = data.get("details", {})
            totals = details.get("totals", {})
            refund_amount = Decimal(totals.get("total", "0")) / 100
            
            logger.info(f"Paddle refund: user={user_id}, amount=${refund_amount}")
            
            return Transaction(
                id=f"txn_refund_{secrets.token_hex(8)}",
                user_id=user_id,
                type=TransactionType.REFUND,
                amount_usd=-refund_amount,  # Negative for refund
                balance_before=Decimal("0"),
                balance_after=Decimal("0"),
                source="paddle",
                external_id=transaction_id,
                metadata={
                    "paddle_transaction_id": transaction_id,
                    "original_transaction_id": data.get("original_transaction_id"),
                },
            )
            
        except Exception as e:
            logger.error(f"Error processing Paddle refund: {e}")
            return None
    
    async def get_payment_status(self, payment_id: str) -> PaymentStatus:
        """Get Paddle transaction status."""
        import logging
        
        logger = logging.getLogger(__name__)
        
        try:
            response = await self._request_with_retry(
                "GET",
                f"/transactions/{payment_id}",
            )
            
            data = response.get("data", {})
            status = data.get("status", "").lower()
            
            # Map Paddle statuses to our enum
            status_map = {
                "draft": PaymentStatus.PENDING,
                "ready": PaymentStatus.PENDING,
                "billed": PaymentStatus.PROCESSING,
                "completed": PaymentStatus.COMPLETED,
                "canceled": PaymentStatus.CANCELLED,
                "past_due": PaymentStatus.FAILED,
            }
            
            return status_map.get(status, PaymentStatus.PENDING)
            
        except ProviderError as e:
            if "not_found" in str(e).lower():
                logger.warning(f"Paddle transaction not found: {payment_id}")
                return PaymentStatus.FAILED
            raise
        except Exception as e:
            logger.error(f"Error getting Paddle status: {e}")
            return PaymentStatus.PENDING
    
    async def refund(self, payment_id: str, amount: Optional[Decimal] = None) -> bool:
        """Refund a Paddle payment."""
        import logging
        
        logger = logging.getLogger(__name__)
        
        try:
            payload = {
                "transaction_id": payment_id,
                "reason": "Customer refund request",
            }
            
            if amount:
                # Partial refund
                payload["type"] = "partial"
                payload["items"] = [
                    {
                        "type": "partial",
                        "amount": str(int(amount * 100)),
                    }
                ]
            else:
                payload["type"] = "full"
            
            await self._request_with_retry(
                "POST",
                "/adjustments",
                json=payload,
            )
            
            logger.info(f"Paddle refund initiated: {payment_id}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to refund Paddle payment: {e}")
            return False


class TelegramStarsProvider(PaymentProvider):
    """
    Telegram Stars payment provider implementation.
    
    Note: Telegram Stars invoices must be created through the Bot API
    (via aiogram), not through HTTP API. This provider:
    - Returns invoice data for bot handlers to create the actual invoice
    - Processes successful_payment callbacks from bot handlers
    """
    
    # Stars to USD conversion (approximate rates)
    # Based on Telegram's pricing: ~50 stars ≈ $1
    STARS_PACKAGES = [
        {"stars": 50, "usd": Decimal("1.00"), "label": "50 ⭐ (~$1)"},
        {"stars": 100, "usd": Decimal("2.00"), "label": "100 ⭐ (~$2)"},
        {"stars": 250, "usd": Decimal("5.00"), "label": "250 ⭐ (~$5)"},
        {"stars": 500, "usd": Decimal("10.00"), "label": "500 ⭐ (~$10)"},
        {"stars": 1000, "usd": Decimal("20.00"), "label": "1000 ⭐ (~$20)"},
        {"stars": 2500, "usd": Decimal("50.00"), "label": "2500 ⭐ (~$50)"},
        {"stars": 5000, "usd": Decimal("100.00"), "label": "5000 ⭐ (~$100)"},
    ]
    
    # Conversion rate: 1 USD = X Stars
    USD_TO_STARS_RATE = 50
    
    def __init__(self, bot_token: str, redis_client=None):
        self.bot_token = bot_token
        self._redis = redis_client
    
    @property
    def name(self) -> str:
        return "telegram_stars"
    
    @classmethod
    def usd_to_stars(cls, usd: Decimal) -> int:
        """Convert USD amount to Stars."""
        return int(usd * cls.USD_TO_STARS_RATE)
    
    @classmethod
    def stars_to_usd(cls, stars: int) -> Decimal:
        """Convert Stars to USD amount."""
        return Decimal(str(stars)) / cls.USD_TO_STARS_RATE
    
    @classmethod
    def get_package_for_usd(cls, usd: Decimal) -> Optional[dict]:
        """Get Stars package matching USD amount."""
        for pkg in cls.STARS_PACKAGES:
            if abs(pkg["usd"] - usd) < Decimal("0.01"):
                return pkg
        return None
    
    async def create_checkout(
        self,
        request: PaymentRequest,
        success_url: str,
        cancel_url: str,
    ) -> PaymentResult:
        """
        Prepare invoice data for Telegram Stars.
        
        Note: Actual invoice must be created via aiogram in bot handler.
        This method returns the data needed to create the invoice.
        """
        from api.services.metrics import PaymentTimer, track_payment_request, track_payment_error
        
        try:
            with PaymentTimer("telegram_stars"):
                # Calculate Stars amount
                stars = self.usd_to_stars(request.amount_usd)
                
                # Find matching package or use custom amount
                package = self.get_package_for_usd(request.amount_usd)
                if package:
                    stars = package["stars"]
                
                # Generate unique payload
                timestamp = int(time.time())
                payload = f"topup:{request.user_id}:{timestamp}:{request.idempotency_key[:8]}"
                
                # Store pending payment data in Redis (if available)
                if self._redis:
                    pending_key = f"pending_stars:{payload}"
                    pending_data = json.dumps({
                        "user_id": request.user_id,
                        "stars": stars,
                        "usd": str(request.amount_usd),
                        "idempotency_key": request.idempotency_key,
                        "created_at": timestamp,
                    })
                    self._redis.setex(pending_key, 3600, pending_data)  # 1 hour expiry
                
                logger.info(f"Stars invoice prepared: user={request.user_id}, {stars} stars (${request.amount_usd})")
                
                track_payment_request("telegram_stars", "success")
                
                # Return invoice data (not a checkout URL)
                # Bot handler will use this to create actual invoice
                return PaymentResult(
                    payment_id=payload,
                    status=PaymentStatus.PENDING,
                    checkout_url=None,  # No URL - handled by bot
                    invoice_link=None,  # Will be set by bot handler
                    metadata={
                        "stars": stars,
                        "usd": str(request.amount_usd),
                        "payload": payload,
                        "title": "KikuAI Balance Top-up",
                        "description": f"Add ${request.amount_usd} to your KikuAI balance",
                    },
                )
            
        except Exception as e:
            track_payment_request("telegram_stars", "failed")
            track_payment_error("telegram_stars", "unknown")
            logger.error(f"Failed to prepare Stars invoice: {e}")
            return PaymentResult(
                payment_id="",
                status=PaymentStatus.FAILED,
                error=str(e),
            )
    
    async def verify_webhook(self, event: WebhookEvent) -> bool:
        """
        Telegram Stars verification is done through Bot API.
        
        The pre_checkout_query in bot handler validates the payment.
        When we receive successful_payment, it's already verified by Telegram.
        """
        return True
    
    async def process_webhook(self, event: WebhookEvent) -> Optional[Transaction]:
        """
        Process successful_payment callback from bot handler.
        
        Event data should contain:
        - user_id: Telegram user ID
        - payload: Invoice payload (topup:user_id:timestamp:key)
        - telegram_payment_charge_id: Telegram payment ID
        - total_amount: Stars amount
        """
        import logging
        import json
        
        logger = logging.getLogger(__name__)
        
        from api.services.metrics import WebhookTimer, track_webhook_event
        
        try:
            with WebhookTimer("telegram_stars"):
                data = event.data
                
                user_id = data.get("user_id")
                payload = data.get("payload", "")
                stars_amount = data.get("total_amount", 0)
                charge_id = data.get("telegram_payment_charge_id", "")
                
                if not user_id:
                    logger.error("Missing user_id in Stars payment")
                    track_webhook_event("telegram_stars", "payment", "failed")
                    return None
                
                # Get pending payment data
                usd_amount = self.stars_to_usd(stars_amount)
                
                if self._redis:
                    pending_key = f"pending_stars:{payload}"
                    pending_data = self._redis.get(pending_key)
                    
                    if pending_data:
                        pending = json.loads(pending_data)
                        # Validate user matches
                        if int(pending.get("user_id")) != int(user_id):
                            logger.warning(f"User mismatch in Stars payment: {user_id} vs {pending.get('user_id')}")
                        # Use stored USD amount for consistency
                        usd_amount = Decimal(pending.get("usd", str(usd_amount)))
                        
                        # Clean up pending data
                        self._redis.delete(pending_key)
                
                logger.info(f"Stars payment success: user={user_id}, {stars_amount} stars (${usd_amount})")
                track_webhook_event("telegram_stars", "payment", "success")
                
                # Create transaction
                return Transaction(
                    id=f"txn_stars_{secrets.token_hex(8)}",
                    user_id=int(user_id),
                    type=TransactionType.TOPUP,
                    amount_usd=usd_amount,
                    balance_before=Decimal("0"),  # Will be set by BalanceManager
                    balance_after=Decimal("0"),   # Will be set by BalanceManager
                    source="telegram_stars",
                    external_id=charge_id,
                    metadata={
                        "telegram_payment_charge_id": charge_id,
                        "stars_amount": stars_amount,
                        "payload": payload,
                    },
                )
            
        except Exception as e:
            logger.error(f"Error processing Stars payment: {e}")
            track_webhook_event("telegram_stars", "payment", "failed")
            return None
    
    async def get_payment_status(self, payment_id: str) -> PaymentStatus:
        """
        Get Stars payment status.
        
        Stars payments are instant - check if processed in Redis.
        """
        import logging
        
        logger = logging.getLogger(__name__)
        
        try:
            if self._redis:
                # Check if pending
                pending_key = f"pending_stars:{payment_id}"
                if self._redis.exists(pending_key):
                    return PaymentStatus.PENDING
                
                # Check if processed
                processed_key = f"stars_processed:{payment_id}"
                if self._redis.exists(processed_key):
                    return PaymentStatus.COMPLETED
            
            # Unknown - assume pending
            return PaymentStatus.PENDING
            
        except Exception as e:
            logger.error(f"Error getting Stars status: {e}")
            return PaymentStatus.PENDING

