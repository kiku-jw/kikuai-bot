"""
Creem Payment Provider.

Implements PaymentProvider interface for Creem.io integration.
Handles checkout creation, webhook verification, and payment processing.
"""

import hashlib
import hmac
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional

import httpx

from api.services.payment_engine import (
    PaymentProvider,
    PaymentRequest,
    PaymentResult,
    PaymentStatus,
    Transaction,
    TransactionType,
    WebhookEvent,
    InvalidSignatureError,
    ProviderError,
)
from config.settings import CREDITS_PER_USD

logger = logging.getLogger(__name__)

# Creem API base URL
CREEM_API_BASE = "https://api.creem.io/v1"


class CreemProvider(PaymentProvider):
    """
    Creem.io payment provider.
    
    Uses Creem's API for creating checkouts and processing
    webhook events for payment completion.
    """
    
    def __init__(
        self,
        api_key: str = None,
        product_id: str = None,
        webhook_secret: str = None,
        balance_service=None,
    ):
        self._api_key = api_key
        self._product_id = product_id
        self._webhook_secret = webhook_secret
        self._balance_service = balance_service
        
        if not self._api_key:
            logger.warning("Creem API key not configured")
    
    @property
    def name(self) -> str:
        return "creem"
    
    async def create_checkout(
        self,
        request: PaymentRequest,
        success_url: str = None,
        cancel_url: str = None,
    ) -> PaymentResult:
        """
        Create a Creem checkout session.
        
        Uses the Creem Checkout API to generate a payment link.
        """
        if not self._api_key or not self._product_id:
            return PaymentResult(
                payment_id="",
                status=PaymentStatus.FAILED,
                error="Creem not configured"
            )
        
        # Calculate credits from USD amount
        credits_amount = int(request.amount_usd * CREDITS_PER_USD)
        
        # Build checkout request
        checkout_data = {
            "product_id": self._product_id,
            "metadata": {
                "user_id": str(request.user_id),
                "credits": credits_amount,
                "idempotency_key": request.idempotency_key,
            },
        }
        
        # Add optional redirect URLs
        if success_url:
            checkout_data["success_url"] = success_url
        if cancel_url:
            checkout_data["cancel_url"] = cancel_url
        
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{CREEM_API_BASE}/checkouts",
                    json=checkout_data,
                    headers={
                        "x-api-key": self._api_key,
                        "Content-Type": "application/json",
                    },
                    timeout=30.0,
                )
                
                if response.status_code in (200, 201):
                    data = response.json()
                    
                    checkout_url = data.get("checkout_url")
                    checkout_id = data.get("id", request.idempotency_key)
                    
                    logger.info(f"Creem checkout created: {checkout_id}")
                    
                    return PaymentResult(
                        payment_id=checkout_id,
                        status=PaymentStatus.PENDING,
                        checkout_url=checkout_url,
                        expires_at=datetime.utcnow() + timedelta(hours=24),
                        metadata={
                            "user_id": request.user_id,
                            "credits": credits_amount,
                        }
                    )
                else:
                    error_msg = response.text[:200]
                    logger.error(f"Creem checkout failed: {response.status_code} - {error_msg}")
                    return PaymentResult(
                        payment_id=request.idempotency_key,
                        status=PaymentStatus.FAILED,
                        error=f"Checkout creation failed: {response.status_code}"
                    )
                    
        except httpx.TimeoutException:
            logger.error("Creem checkout timeout")
            return PaymentResult(
                payment_id=request.idempotency_key,
                status=PaymentStatus.FAILED,
                error="Payment service timeout"
            )
        except Exception as e:
            logger.error(f"Creem checkout error: {e}")
            return PaymentResult(
                payment_id=request.idempotency_key,
                status=PaymentStatus.FAILED,
                error=str(e)
            )
    
    async def verify_webhook(self, event: WebhookEvent) -> bool:
        """
        Verify Creem webhook signature.
        
        Creem uses HMAC-SHA256 for webhook verification.
        """
        if not self._webhook_secret:
            logger.warning("Creem webhook secret not configured")
            return False
        
        try:
            expected_signature = hmac.new(
                self._webhook_secret.encode("utf-8"),
                event.raw_body,
                hashlib.sha256
            ).hexdigest()
            
            # Handle signature format (may have prefix)
            provided = event.signature
            if provided.startswith("sha256="):
                provided = provided[7:]
            
            return hmac.compare_digest(expected_signature, provided)
            
        except Exception as e:
            logger.error(f"Webhook signature verification error: {e}")
            return False
    
    async def process_webhook(self, event: WebhookEvent) -> Optional[Transaction]:
        """
        Process Creem webhook event.
        
        Handles checkout.completed event to credit user accounts.
        """
        # Verify signature first
        if not await self.verify_webhook(event):
            raise InvalidSignatureError("Invalid webhook signature")
        
        event_type = event.event_type
        logger.info(f"Processing Creem webhook: {event_type}")
        
        # Only process checkout completion events
        if event_type not in ("checkout.completed", "payment.successful", "order.completed"):
            logger.debug(f"Ignoring event type: {event_type}")
            return None
        
        # Extract payment data
        payment_data = event.data
        payment_id = payment_data.get("id", "")
        
        # Get metadata with user info
        metadata = payment_data.get("metadata", {})
        
        user_id = metadata.get("user_id")
        credits = metadata.get("credits")
        idempotency_key = metadata.get("idempotency_key")
        
        if not user_id or not credits:
            logger.warning(f"Missing user_id or credits in webhook: {payment_id}")
            return None
        
        # Get payment amount (Creem returns cents)
        amount_cents = payment_data.get("amount", 0)
        total_usd = Decimal(str(amount_cents)) / 100
        
        # Fallback to credits-based calculation if amount not in response
        if total_usd == 0:
            total_usd = Decimal(str(credits)) / CREDITS_PER_USD
        
        logger.info(
            f"Creem payment completed: payment={payment_id}, "
            f"user={user_id}, credits={credits}, amount=${total_usd}"
        )
        
        # Credit the user's balance
        if self._balance_service:
            try:
                transaction = await self._balance_service.credit_balance(
                    user_id=int(user_id),
                    amount_usd=total_usd,
                    source=f"creem:{payment_id}",
                    metadata={
                        "payment_id": payment_id,
                        "credits": credits,
                        "idempotency_key": idempotency_key,
                    }
                )
                return transaction
            except Exception as e:
                logger.error(f"Failed to credit balance: {e}")
                raise
        
        # Return a transaction record even without balance service
        return Transaction(
            id=f"creem_{payment_id}",
            user_id=int(user_id),
            type=TransactionType.TOPUP,
            amount_usd=total_usd,
            balance_before=Decimal("0"),
            balance_after=total_usd,
            source=f"creem:{payment_id}",
            external_id=payment_id,
            metadata={
                "credits": credits,
                "idempotency_key": idempotency_key,
            }
        )
    
    async def get_payment_status(self, payment_id: str) -> PaymentStatus:
        """Get payment status from Creem."""
        # Would need to query Creem API - for now return pending
        return PaymentStatus.PENDING
    
    async def refund(self, payment_id: str, amount: Optional[Decimal] = None) -> bool:
        """
        Refund a Creem payment.
        
        Creem supports refunds through the API.
        """
        if not self._api_key:
            return False
        
        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    f"{CREEM_API_BASE}/refunds",
                    json={"payment_id": payment_id},
                    headers={
                        "x-api-key": self._api_key,
                        "Content-Type": "application/json",
                    },
                    timeout=30.0,
                )
                
                if response.status_code in (200, 201):
                    logger.info(f"Creem refund successful: {payment_id}")
                    return True
                else:
                    logger.error(f"Creem refund failed: {response.status_code}")
                    return False
                    
        except Exception as e:
            logger.error(f"Creem refund error: {e}")
            return False
