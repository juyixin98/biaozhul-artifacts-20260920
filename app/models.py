from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

# Wallet kinds
WALLET_CUSTODIAL = "custodial"
WALLET_WATCH_ONLY = "watch_only"
WALLET_KINDS = (WALLET_CUSTODIAL, WALLET_WATCH_ONLY)

# Draft lifecycle
DRAFT_OPEN = "open"
DRAFT_FROZEN = "frozen"

# Sign request lifecycle
REQ_PENDING = "pending"
REQ_SUCCESS = "success"
REQ_FAILED = "failed"
REQ_RELEASED = "released"
REQ_STATES = (REQ_PENDING, REQ_SUCCESS, REQ_FAILED, REQ_RELEASED)


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    # Only a SHA-256 hex digest of the API key is stored. The plaintext key is
    # returned exactly once, at creation time.
    api_key_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    wallets: Mapped[list["Wallet"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # never print anything key-like
        return f"<User id={self.id} name={self.name!r}>"


class Wallet(Base):
    __tablename__ = "wallets"
    __table_args__ = (
        UniqueConstraint("user_id", "address", name="uq_wallets_user_address"),
    )

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    address: Mapped[str] = mapped_column(String(42), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # AES-256-GCM blob: 12-byte nonce || ciphertext-with-tag. NULL for
    # watch-only wallets. The plaintext private key never leaves the signing
    # boundary and is intentionally absent from __repr__/responses/logs.
    key_ciphertext: Mapped[Optional[bytes]] = mapped_column(
        LargeBinary, nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    user: Mapped["User"] = relationship(back_populates="wallets")

    def __repr__(self) -> str:  # must never expose key material
        return (
            f"<Wallet id={self.id} kind={self.kind} "
            f"address={self.address} has_key={self.key_ciphertext is not None}>"
        )


class Draft(Base):
    __tablename__ = "drafts"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    wallet_id: Mapped[str] = mapped_column(
        ForeignKey("wallets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chain_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    to_address: Mapped[str] = mapped_column(String(42), nullable=False)
    value_wei: Mapped[int] = mapped_column(Numeric(78, 0), nullable=False)
    gas: Mapped[int] = mapped_column(BigInteger, nullable=False)
    gas_price_wei: Mapped[int] = mapped_column(Numeric(78, 0), nullable=False)
    nonce: Mapped[int] = mapped_column(BigInteger, nullable=False)
    data_hex: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=DRAFT_OPEN)
    # Set once the draft is submitted; afterwards content is immutable.
    frozen_at: Mapped[Optional[dt.datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
    )


class SignRequest(Base):
    __tablename__ = "sign_requests"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "idempotency_key", name="uq_signrequests_user_idem"
        ),
        # At most one occupying (pending/success) request per wallet+chain+nonce.
        Index(
            "uq_signrequests_nonce_occupying",
            "wallet_id",
            "chain_id",
            "nonce",
            unique=True,
            postgresql_where=(
                "status IN ('pending', 'success')"
            ),
        ),
        CheckConstraint("status IN ('pending', 'success', 'failed', 'released')",
                        name="ck_signrequests_status"),
        CheckConstraint("value_wei >= 0 AND gas_price_wei >= 0 AND gas > 0",
                        name="ck_signrequests_nonneg"),
    )

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    wallet_id: Mapped[str] = mapped_column(
        ForeignKey("wallets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    draft_id: Mapped[str] = mapped_column(
        ForeignKey("drafts.id", ondelete="RESTRICT"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    # Canonical hash of (wallet, chain, to, value, gas, gas_price, nonce, data).
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    chain_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    to_address: Mapped[str] = mapped_column(String(42), nullable=False)
    value_wei: Mapped[int] = mapped_column(Numeric(78, 0), nullable=False)
    gas: Mapped[int] = mapped_column(BigInteger, nullable=False)
    gas_price_wei: Mapped[int] = mapped_column(Numeric(78, 0), nullable=False)
    nonce: Mapped[int] = mapped_column(BigInteger, nullable=False)
    data_hex: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default=REQ_PENDING)
    # Signature artefacts (success only). tx_hash is keccak256 of the signed
    # RLP payload; raw_transaction_hex broadcasts the offline signature.
    raw_transaction_hex: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tx_hash: Mapped[Optional[str]] = mapped_column(String(66), nullable=True)
    error_reason: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)

    # UTC calendar day of the hold (date string YYYY-MM-DD), used for the daily
    # quota accounting; null once released/failed so the row stops counting.
    quota_day: Mapped[Optional[str]] = mapped_column(String(10), nullable=True, index=True)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, index=True
    )
    signed_at: Mapped[Optional[dt.datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[Optional[dt.datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AuditLog(Base):
    """Append-only audit trail. Stores transaction summaries and results only -
    never private keys or API keys."""

    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    actor_request_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    result: Mapped[str] = mapped_column(String(16), nullable=False)
    wallet_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    sign_request_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    # Transaction *summary* only: chain/nonce/value/to/tx_hash - no secrets.
    summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    detail: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        server_default=func.now(),
        index=True,
    )
