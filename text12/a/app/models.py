from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def gen_uuid() -> uuid.UUID:
    return uuid.uuid4()


class Base(DeclarativeBase):
    pass


class LeaseStatus(str, enum.Enum):
    active = "active"
    closed = "closed"        # 设备正常关闭
    expired = "expired"      # 心跳超时被清理
    revoked = "revoked"      # 设备被撤销
    superseded = "superseded"  # 被同设备的新会话取代（不应出现，仅作安全网）


class DeviceStatus(str, enum.Enum):
    active = "active"
    revoked = "revoked"


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    admin_key_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    access_points: Mapped[list["AccessPoint"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")
    pools: Mapped[list["AddressPool"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")
    devices: Mapped[list["Device"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")


class AccessPoint(Base):
    __tablename__ = "access_points"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_ap_tenant_name"),
        CheckConstraint("capacity >= 0", name="ck_ap_capacity_nonneg"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # 最大并发设备会话数
    capacity: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    tenant: Mapped[Tenant] = relationship(back_populates="access_points")
    pools: Mapped[list["AddressPool"]] = relationship(back_populates="access_point")


class AddressPool(Base):
    """接入点下的 IPv4 地址段，cidr 形如 10.0.0.0/24。

    可分配集合在分配时动态计算：排除网络地址、广播地址，以及 reserved_first
    个段首保留地址和 reserved_last 个段尾保留地址（均不与网络/广播重复扣除）。
    """

    __tablename__ = "address_pools"
    __table_args__ = (
        UniqueConstraint("access_point_id", "cidr", name="uq_pool_ap_cidr"),
        CheckConstraint("reserved_first >= 0 AND reserved_last >= 0", name="ck_pool_reserved_nonneg"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    access_point_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("access_points.id", ondelete="CASCADE"), nullable=False
    )
    cidr: Mapped[str] = mapped_column(String(64), nullable=False)
    reserved_first: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    reserved_last: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    tenant: Mapped[Tenant] = relationship(back_populates="pools")
    access_point: Mapped[AccessPoint] = relationship(back_populates="pools")


class Device(Base):
    __tablename__ = "devices"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_device_tenant_name"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[DeviceStatus] = mapped_column(
        Enum(DeviceStatus, name="device_status", values_callable=lambda e: [m.value for m in e]),
        default=DeviceStatus.active,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    tenant: Mapped[Tenant] = relationship(back_populates="devices")


class Lease(Base):
    """一次设备会话 + 一个 IP 租约。代次（generation）在每次建立会话时递增。"""

    __tablename__ = "leases"
    __table_args__ = (
        # 同一接入点内一个地址至多被一条活动租约占用（部分唯一索引）
        Index(
            "uq_lease_active_ap_ip",
            "access_point_id",
            "ip_address",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        # 同一设备至多一条活动租约
        Index(
            "uq_lease_active_device",
            "device_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index("ix_lease_ap_status", "access_point_id", "status"),
        Index("ix_lease_last_seen", "last_seen_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    access_point_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("access_points.id", ondelete="CASCADE"), nullable=False
    )
    device_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
    )
    pool_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("address_pools.id", ondelete="RESTRICT"), nullable=False
    )
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    ip_address: Mapped[str] = mapped_column(INET, nullable=False)
    status: Mapped[LeaseStatus] = mapped_column(
        Enum(LeaseStatus, name="lease_status", values_callable=lambda e: [m.value for m in e]),
        default=LeaseStatus.active,
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False)

    connected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    termination_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # 关闭时的快照（原因、时间、旧代次心跳等），用于审计与排查
    events: Mapped[list["LeaseEvent"]] = relationship(
        back_populates="lease", cascade="all, delete-orphan"
    )


class LeaseEvent(Base):
    """租约生命周期事件审计：created / heartbeat（采样）/ rejected / terminated。"""

    __tablename__ = "lease_events"
    __table_args__ = (Index("ix_lease_event_lease", "lease_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    lease_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("leases.id", ondelete="SET NULL"), nullable=True
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    lease: Mapped[Lease | None] = relationship(back_populates="events")
