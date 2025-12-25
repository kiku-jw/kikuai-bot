from datetime import datetime
from decimal import Decimal
from typing import List, Optional
from uuid import UUID, uuid4

from sqlalchemy import ARRAY, BigInteger, Boolean, DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncAttrs, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from config.settings import POSTGRES_URL

# statement_cache_size=0 is required for pgbouncer/Supavisor compatibility
# (they don't support prepared statements in transaction mode)
engine = create_async_engine(
    POSTGRES_URL, 
    echo=False,
    connect_args={"prepared_statement_cache_size": 0, "statement_cache_size": 0}
)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(AsyncAttrs, DeclarativeBase):
    pass

class Account(Base):
    __tablename__ = "accounts"
    
    id: Mapped[UUID] = mapped_column(PG_UUID, primary_key=True, default=uuid4)
    telegram_id: Mapped[Optional[int]] = mapped_column(BigInteger, unique=True, nullable=True)  # nullable for email-only users
    email: Mapped[Optional[str]] = mapped_column(Text, unique=True, nullable=True)  # unique for email login
    balance_usd: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=Decimal("0.00000000"))
    
    # B2B Features
    auto_recharge_threshold: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 8))
    auto_recharge_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(18, 8))
    opt_in_debug: Mapped[bool] = mapped_column(Boolean, default=False)
    
    # Auth Fallback (Email Magic Link)
    email_auth_token: Mapped[Optional[str]] = mapped_column(Text, unique=True)
    email_auth_expires: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    last_active_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    
    keys: Mapped[List["APIKey"]] = relationship("APIKey", back_populates="account", cascade="all, delete-orphan")
    transactions: Mapped[List["Transaction"]] = relationship("Transaction", back_populates="account")
    usage_logs: Mapped[List["UsageLog"]] = relationship("UsageLog", back_populates="account")
    audit_logs: Mapped[List["AuditLog"]] = relationship("AuditLog", back_populates="account")
    debug_logs: Mapped[List["DebugLog"]] = relationship("DebugLog", back_populates="account", cascade="all, delete-orphan")

class Product(Base):
    __tablename__ = "products"
    
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    base_price_per_unit: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    unit_name: Mapped[str] = mapped_column(Text, default="request")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

class APIKey(Base):
    __tablename__ = "api_keys"
    
    id: Mapped[UUID] = mapped_column(PG_UUID, primary_key=True, default=uuid4)
    account_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    key_prefix: Mapped[str] = mapped_column(Text, nullable=False) # e.g. 'kikusm'
    key_hash: Mapped[str] = mapped_column(Text, unique=True, nullable=False) # HMAC-SHA256
    label: Mapped[Optional[str]] = mapped_column(Text)
    scopes: Mapped[List[str]] = mapped_column(ARRAY(Text), server_default='{}')
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    
    account: Mapped["Account"] = relationship("Account", back_populates="keys")

class Transaction(Base):
    __tablename__ = "transactions"
    
    id: Mapped[UUID] = mapped_column(PG_UUID, primary_key=True, default=uuid4)
    account_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False)
    amount_usd: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False) # 'topup', 'usage', 'adjustment'
    product_id: Mapped[Optional[str]] = mapped_column(ForeignKey("products.id"))
    idempotency_key: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    
    account: Mapped["Account"] = relationship("Account", back_populates="transactions")

class UsageLog(Base):
    __tablename__ = "usage_logs"
    
    id: Mapped[UUID] = mapped_column(PG_UUID, primary_key=True, default=uuid4)
    account_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id", ondelete="RESTRICT"), nullable=False)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"), nullable=False)
    units_consumed: Mapped[int] = mapped_column(nullable=False)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    metadata_json: Mapped[Optional[dict]] = mapped_column(JSONB)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    
    account: Mapped["Account"] = relationship("Account", back_populates="usage_logs")

class AuditLog(Base):
    __tablename__ = "audit_logs"
    
    id: Mapped[UUID] = mapped_column(PG_UUID, primary_key=True, default=uuid4)
    account_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    action: Mapped[str] = mapped_column(Text, nullable=False) # 'CREATE_KEY', 'REVOKE_KEY', etc.
    actor_id: Mapped[Optional[str]] = mapped_column(Text) # Telegram ID of the actor
    request_id: Mapped[Optional[str]] = mapped_column(Text)
    ip_address: Mapped[Optional[str]] = mapped_column(Text)
    user_agent: Mapped[Optional[str]] = mapped_column(Text)
    metadata_json: Mapped[Optional[dict]] = mapped_column(JSONB)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    
    account: Mapped["Account"] = relationship("Account", back_populates="audit_logs")

class DebugLog(Base):
    __tablename__ = "debug_logs"
    
    id: Mapped[UUID] = mapped_column(PG_UUID, primary_key=True, default=uuid4)
    account_id: Mapped[UUID] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False)
    request_id: Mapped[Optional[str]] = mapped_column(Text)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    method: Mapped[str] = mapped_column(Text, nullable=False)
    request_body: Mapped[Optional[str]] = mapped_column(Text)
    response_body: Mapped[Optional[str]] = mapped_column(Text)
    status_code: Mapped[int] = mapped_column(nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    
    account: Mapped["Account"] = relationship("Account", back_populates="debug_logs")

async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
