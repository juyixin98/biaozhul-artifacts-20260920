from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)

from .db import Base


class User(Base):
    __tablename__ = "users"

    id = Column(String(36), primary_key=True)
    api_key = Column(String(64), unique=True, nullable=False, index=True)
    created_at = Column(DateTime, nullable=False)


class Wallet(Base):
    __tablename__ = "wallets"

    id = Column(String(36), primary_key=True)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    address = Column(String(42), nullable=False)
    kind = Column(String(16), nullable=False)  # "custodial" | "watch_only"
    # AES-GCM(master_key) ciphertext, base64(nonce||ct). NULL for watch-only.
    # Never exposed through the API, logs, or audit records.
    encrypted_key = Column(Text, nullable=True)
    last_signed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False)


class Draft(Base):
    __tablename__ = "drafts"

    id = Column(String(36), primary_key=True)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    wallet_id = Column(String(36), ForeignKey("wallets.id"), nullable=False, index=True)
    chain_id = Column(Integer, nullable=False)
    to_address = Column(String(42), nullable=False)
    value_wei = Column(Numeric(78, 0), nullable=False)
    gas_limit = Column(BigInteger, nullable=False)
    gas_price_wei = Column(Numeric(78, 0), nullable=False)
    nonce = Column(BigInteger, nullable=False)
    status = Column(String(16), nullable=False, default="draft")  # draft | submitted
    # sha256 over the canonical frozen content; set at submit time.
    content_hash = Column(String(64), nullable=True)
    created_at = Column(DateTime, nullable=False)
    submitted_at = Column(DateTime, nullable=True)


class SignRequest(Base):
    __tablename__ = "sign_requests"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_sign_idempotency"),
        Index("ix_sign_wallet_chain_nonce", "wallet_id", "chain_id", "nonce"),
    )

    id = Column(String(36), primary_key=True)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    wallet_id = Column(String(36), ForeignKey("wallets.id"), nullable=False)
    draft_id = Column(String(36), ForeignKey("drafts.id"), nullable=False)
    idempotency_key = Column(String(128), nullable=False)
    request_hash = Column(String(64), nullable=False)
    chain_id = Column(Integer, nullable=False)
    nonce = Column(BigInteger, nullable=False)
    status = Column(String(16), nullable=False, default="pending")  # pending | succeeded | failed
    signed_tx = Column(Text, nullable=True)
    tx_hash = Column(String(66), nullable=True)
    error = Column(Text, nullable=True)
    quota_reserved_wei = Column(Numeric(78, 0), nullable=False)
    quota_released = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)


class DailyUsage(Base):
    __tablename__ = "daily_usage"

    wallet_id = Column(String(36), ForeignKey("wallets.id"), primary_key=True)
    day = Column(String(10), primary_key=True)  # UTC date, YYYY-MM-DD
    used_wei = Column(Numeric(78, 0), nullable=False, default=0)
    limit_wei = Column(Numeric(78, 0), nullable=False)


class AuditLog(Base):
    """Traceable audit trail: transaction digests and operation results only.

    Never stores plaintext or ciphertext key material.
    """

    __tablename__ = "audit_log"

    id = Column(String(36), primary_key=True)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    wallet_id = Column(String(36), nullable=True)
    action = Column(String(32), nullable=False)
    tx_digest = Column(String(66), nullable=True)
    result = Column(String(16), nullable=False)  # ok | error
    detail = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False)
