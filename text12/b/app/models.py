from __future__ import annotations

import datetime as dt
import uuid
from typing import Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class AdminUser(Base):
    """Platform super-admins (tenant_id NULL) and per-tenant admins."""

    __tablename__ = "admin_users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    username: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    tenant_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("tenants.id", ondelete="RESTRICT"), nullable=True
    )
    is_platform_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class AddressPool(Base):
    __tablename__ = "address_pools"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    cidr: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_pool_tenant_name"),)


class PoolAddress(Base):
    """Every address of a pool materialised as a row.

    kind: "usable" | "network" | "broadcast"
    reserved: operator-claimed addresses (e.g. gateways) — never allocated
    status:   "free"    | "allocated" | "released"
    """

    __tablename__ = "pool_addresses"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    pool_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("address_pools.id", ondelete="CASCADE"), nullable=False
    )
    ip: Mapped[str] = mapped_column(INET, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    reserved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="free", nullable=False)

    __table_args__ = (
        UniqueConstraint("pool_id", "ip", name="uq_pool_address_ip"),
        # One physical address can only be leased once at a time.
        Index(
            "uq_pool_allocated_address",
            "pool_id",
            "ip",
            unique=True,
            postgresql_where="status = 'allocated'",
        ),
        CheckConstraint("kind IN ('usable','network','broadcast')", name="ck_pool_address_kind"),
        CheckConstraint("status IN ('free','allocated','released')", name="ck_pool_address_status"),
    )


class AccessPoint(Base):
    __tablename__ = "access_points"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    pool_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("address_pools.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    capacity: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_ap_tenant_name"),
        CheckConstraint("capacity > 0", name="ck_ap_capacity_positive"),
    )


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    token_hash: Mapped[Optional[str]] = mapped_column(String(128), unique=True, nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_device_tenant_name"),)


class Session(Base):
    """A device connection (generation) to an access point."""

    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    device_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
    )
    access_point_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("access_points.id", ondelete="RESTRICT"), nullable=False
    )
    pool_address_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pool_addresses.id", ondelete="RESTRICT"), nullable=False
    )
    generation: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    lease_token_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    ip: Mapped[str] = mapped_column(INET, nullable=False)
    connected_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_heartbeat_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # At most one active session per device — database-enforced.
        Index(
            "uq_active_session_per_device",
            "device_id",
            unique=True,
            postgresql_where="status = 'active'",
        ),
        Index("ix_sessions_tenant", "tenant_id"),
        CheckConstraint("status IN ('active','closed')", name="ck_session_status"),
        CheckConstraint("generation >= 1", name="ck_session_generation"),
    )


class LeaseTermination(Base):
    """Authoritative, write-once record that a lease has ended.

    The unique constraint on session_id guarantees a lease is released
    exactly once even when close/expiry/revocation race each other.
    """

    __tablename__ = "lease_terminations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=_uuid)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    terminated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("session_id", name="uq_termination_session"),
        CheckConstraint(
            "reason IN ('client_close','heartbeat_timeout','revoked','reconnect','capacity_admin')",
            name="ck_termination_reason",
        ),
    )
