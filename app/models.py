from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class LeaseStatus(str, enum.Enum):
    active = "active"
    closed = "closed"


class LeaseReason(str, enum.Enum):
    connected = "connected"
    device_disconnect = "device_disconnect"
    expired = "expired"
    revoked = "revoked"


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    admin_key_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    pools: Mapped[list["AddressPool"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")
    access_points: Mapped[list["AccessPoint"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")
    devices: Mapped[list["Device"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")


class AddressPool(Base):
    __tablename__ = "address_pools"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    cidr: Mapped[str] = mapped_column(String(64), nullable=False)
    # Explicitly reserved host addresses (e.g. static servers), always excluded.
    reserved_ips: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    tenant: Mapped[Tenant] = relationship(back_populates="pools")
    access_points: Mapped[list["AccessPoint"]] = relationship(back_populates="pool")

    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_pool_tenant_name"),)


class AccessPoint(Base):
    __tablename__ = "access_points"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    pool_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("address_pools.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # Maximum number of simultaneously active leases behind this AP.
    capacity: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    tenant: Mapped[Tenant] = relationship(back_populates="access_points")
    pool: Mapped[AddressPool] = relationship(back_populates="access_points")
    leases: Mapped[list["Lease"]] = relationship(back_populates="access_point")

    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_ap_tenant_name"),)


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    tenant: Mapped[Tenant] = relationship(back_populates="devices")
    leases: Mapped[list["Lease"]] = relationship(back_populates="device")

    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_device_tenant_name"),)


class Lease(Base):
    __tablename__ = "leases"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    device_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
    )
    access_point_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("access_points.id", ondelete="CASCADE"), nullable=False
    )
    ip_address: Mapped[str] = mapped_column(String(64), nullable=False)
    # Bumped on every (re)connect; heartbeats/closes must present the current one.
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    status: Mapped[LeaseStatus] = mapped_column(
        Enum(LeaseStatus, name="lease_status"), default=LeaseStatus.active, nullable=False
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    close_reason: Mapped[LeaseReason | None] = mapped_column(Enum(LeaseReason, name="lease_reason"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    device: Mapped[Device] = relationship(back_populates="leases")
    access_point: Mapped[AccessPoint] = relationship(back_populates="leases")
    events: Mapped[list["LeaseEvent"]] = relationship(
        back_populates="lease", cascade="all, delete-orphan", order_by="LeaseEvent.id"
    )

    # Partial unique indexes (created in the migration) enforce the hard rules:
    #  - one active lease per device
    #  - one active lease per (access point, ip)
    #  - one lease per (device, idempotency key)
    __table_args__ = (
        Index(
            "uq_active_lease_device",
            "device_id",
            unique=True,
            postgresql_where=(status == LeaseStatus.active.name),
        ),
        Index(
            "uq_active_lease_ap_ip",
            "access_point_id",
            "ip_address",
            unique=True,
            postgresql_where=(status == LeaseStatus.active.name),
        ),
        Index(
            "uq_lease_device_idempotency_key",
            "device_id",
            "idempotency_key",
            unique=True,
            postgresql_where=(idempotency_key.isnot(None)),
        ),
    )


class LeaseEvent(Base):
    __tablename__ = "lease_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    lease_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("leases.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)  # connected | closed
    reason: Mapped[LeaseReason] = mapped_column(Enum(LeaseReason, name="lease_reason"), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    lease: Mapped[Lease] = relationship(back_populates="events")
