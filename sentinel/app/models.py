"""SQLAlchemy ORM models for Sentinel."""
from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class EventType(str, enum.Enum):
    file_access = "file_access"
    file_download = "file_download"
    usb_connect = "usb_connect"
    login = "login"
    website_visit = "website_visit"


class UserRole(str, enum.Enum):
    admin = "admin"
    analyst = "analyst"
    manager = "manager"
    employee = "employee"


class AlertRule(str, enum.Enum):
    off_hours_access = "off_hours_access"
    download_burst = "download_burst"
    usb_first = "usb_first"
    file_access_baseline = "file_access_baseline"


class AlertStatus(str, enum.Enum):
    open = "open"
    confirmed = "confirmed"
    false_positive = "false_positive"
    investigated = "investigated"


class BaselineStatus(str, enum.Enum):
    ok = "ok"
    insufficient_history = "insufficient_history"
    zero_variance = "zero_variance"


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # Materialized ancestry path, "/<id>/.../<id>/"; prefix LIKE gives subtree.
    path: Mapped[str] = mapped_column(String(2000), nullable=False, index=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True
    )

    parent: Mapped["Organization | None"] = relationship(remote_side=[id], back_populates="children")
    children: Mapped[list["Organization"]] = relationship(back_populates="parent")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(200), nullable=False)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    role: Mapped[UserRole] = mapped_column(SAEnum(UserRole, name="user_role"), nullable=False)
    org_id: Mapped[int | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True
    )
    manager_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    org: Mapped[Organization | None] = relationship()
    manager: Mapped["User | None"] = relationship(remote_side=[id])


class DepartmentMembership(Base):
    """Departments an analyst is authorized to see."""

    __tablename__ = "department_memberships"
    __table_args__ = (UniqueConstraint("user_id", "department_id", name="uq_depmem_user_department"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    department_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )

    department: Mapped[Organization] = relationship()


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    device_key: Mapped[str] = mapped_column(String(200), nullable=False, unique=True, index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False, default="")

    user: Mapped[User] = relationship()


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("device_id", "event_id", name="uq_event_device_eventid"),
        Index("ix_events_user_type_time", "user_id", "type", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    device_id: Mapped[int] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
    )
    # Denormalized for per-employee detection queries.
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    event_id: Mapped[str] = mapped_column(String(200), nullable=False)
    type: Mapped[EventType] = mapped_column(SAEnum(EventType, name="event_type"), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class Baseline(Base):
    """A versioned file-access baseline. Recomputation inserts a new version, never updates."""

    __tablename__ = "baselines"
    __table_args__ = (
        UniqueConstraint("user_id", "metric", "version", name="uq_baseline_user_metric_version"),
        Index("ix_baseline_user_metric", "user_id", "metric"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    metric: Mapped[str] = mapped_column(String(64), nullable=False, default="file_access_daily")
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    # The day (user tz) whose previous 14 complete days feed this baseline.
    target_date: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    status: Mapped[BaselineStatus] = mapped_column(
        SAEnum(BaselineStatus, name="baseline_status"), nullable=False
    )
    days_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    mean: Mapped[float | None] = mapped_column(Float, nullable=True)
    stddev: Mapped[float | None] = mapped_column(Float, nullable=True)
    daily_counts: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    observed_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    zscore: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Alert(Base):
    __tablename__ = "alerts"
    __table_args__ = (
        UniqueConstraint("dedup_key", name="uq_alert_dedup_key"),
        Index("ix_alert_user_status", "user_id", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    rule: Mapped[AlertRule] = mapped_column(SAEnum(AlertRule, name="alert_rule"), nullable=False)
    status: Mapped[AlertStatus] = mapped_column(
        SAEnum(AlertStatus, name="alert_status"), nullable=False, default=AlertStatus.open
    )
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="medium")
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    device_id: Mapped[int | None] = mapped_column(
        ForeignKey("devices.id", ondelete="SET NULL"), nullable=True
    )
    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Idempotency / de-duplication identity, independent of ingestion order.
    dedup_key: Mapped[str] = mapped_column(String(300), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    baseline_id: Mapped[int | None] = mapped_column(
        ForeignKey("baselines.id", ondelete="SET NULL"), nullable=True
    )
    # Optimistic concurrency token for triage updates.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    baseline: Mapped[Baseline | None] = relationship()


class AlertInvestigation(Base):
    """Immutable audit trail of triage actions. Recomputation never touches these."""

    __tablename__ = "alert_investigations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    alert_id: Mapped[int] = mapped_column(
        ForeignKey("alerts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    action: Mapped[AlertStatus] = mapped_column(SAEnum(AlertStatus, name="alert_status"), nullable=False)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    from_version: Mapped[int] = mapped_column(Integer, nullable=False)
    to_version: Mapped[int] = mapped_column(Integer, nullable=False)
    evidence_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
