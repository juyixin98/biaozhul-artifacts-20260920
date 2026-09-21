"""SQLAlchemy ORM models for CareForce scheduling."""

from __future__ import annotations

import enum
from datetime import datetime, time

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Column,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


# --- identity / authorization ---------------------------------------------


class Coordinator(Base):
    __tablename__ = "coordinators"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    units: Mapped[list["Unit"]] = relationship(
        secondary="coordinator_units", back_populates="coordinators"
    )


class Unit(Base):
    """Authorisation unit (e.g. a care team / ward)."""

    __tablename__ = "units"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)

    coordinators: Mapped[list[Coordinator]] = relationship(
        secondary="coordinator_units", back_populates="units"
    )


coordinator_units = Table(
    "coordinator_units",
    Base.metadata,
    Column("coordinator_id", ForeignKey("coordinators.id", ondelete="CASCADE"), primary_key=True),
    Column("unit_id", ForeignKey("units.id", ondelete="CASCADE"), primary_key=True),
)


class CareWorker(Base):
    __tablename__ = "care_workers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    unit_id: Mapped[int] = mapped_column(ForeignKey("units.id"), nullable=False)
    # Weekly capacity is measured in the worker's home timezone.
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    qualifications: Mapped[list["Qualification"]] = relationship(
        back_populates="worker", cascade="all, delete-orphan"
    )


class Qualification(Base):
    """A qualification code valid for the half-open coverage window.

    A qualification covers a task iff ``valid_from <= task.start`` and
    ``valid_until >= task.end`` (inclusive on both edges), i.e. it must be
    valid across the *entire* task, so a qualification expiring mid-shift
    does not qualify the worker.
    """

    __tablename__ = "qualifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    worker_id: Mapped[int] = mapped_column(
        ForeignKey("care_workers.id", ondelete="CASCADE"), nullable=False
    )
    code: Mapped[str] = mapped_column(String(100), nullable=False)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    worker: Mapped[CareWorker] = relationship(back_populates="qualifications")

    __table_args__ = (
        Index("ix_qualifications_worker_code", "worker_id", "code"),
        CheckConstraint("valid_until > valid_from", name="ck_qualification_window"),
    )


# --- planning ---------------------------------------------------------------


class Recurrence(str, enum.Enum):
    daily = "daily"
    weekly = "weekly"


class PlanStatus(str, enum.Enum):
    active = "active"
    cancelled = "cancelled"


class CarePlan(Base):
    __tablename__ = "care_plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Bumped on every edit; generated tasks snapshot the version they came from.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[PlanStatus] = mapped_column(
        Enum(PlanStatus, name="plan_status"), nullable=False, default=PlanStatus.active
    )
    care_recipient_id: Mapped[str] = mapped_column(String(100), nullable=False)
    unit_id: Mapped[int] = mapped_column(ForeignKey("units.id"), nullable=False)

    service_timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    recurrence: Mapped[Recurrence] = mapped_column(
        Enum(Recurrence, name="recurrence"), nullable=False
    )
    # Monday=0 ... Sunday=6; required for weekly plans, ignored for daily.
    day_of_week: Mapped[int | None] = mapped_column(Integer, nullable=True)
    window_start: Mapped[time] = mapped_column(Time, nullable=False)
    window_end: Mapped[time] = mapped_column(Time, nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    required_qualifications: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, default=list
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "recurrence = 'daily' OR day_of_week BETWEEN 0 AND 6",
            name="ck_plan_weekly_day",
        ),
        CheckConstraint("duration_minutes > 0", name="ck_plan_duration"),
        CheckConstraint("window_end > window_start", name="ck_plan_window"),
    )


plan_prerequisites = Table(
    "plan_prerequisites",
    Base.metadata,
    # Tasks generated from plan_id may only start once the same-day task
    # generated from prerequisite_plan_id for the same recipient is complete.
    Column(
        "plan_id",
        ForeignKey("care_plans.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "prerequisite_plan_id",
        ForeignKey("care_plans.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


class TaskStatus(str, enum.Enum):
    pending = "pending"        # generated, no active offer
    invited = "invited"        # an outstanding invitation exists
    assigned = "assigned"      # accepted, locked in
    completed = "completed"
    cancelled = "cancelled"    # plan cancelled / coordinator cancelled


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(
        ForeignKey("care_plans.id", ondelete="CASCADE"), nullable=False
    )
    plan_version: Mapped[int] = mapped_column(Integer, nullable=False)
    care_recipient_id: Mapped[str] = mapped_column(String(100), nullable=False)
    unit_id: Mapped[int] = mapped_column(ForeignKey("units.id"), nullable=False)

    # Service-local date the visit belongs to; together with plan_id this is the
    # natural idempotency key that makes regeneration a no-op for existing work.
    occurrence_key: Mapped[str] = mapped_column(String(32), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus, name="task_status"), nullable=False, default=TaskStatus.pending
    )
    prerequisite_task_ids: Mapped[list[int]] = mapped_column(
        ARRAY(Integer), nullable=False, default=list
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    assignments: Mapped[list["Assignment"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("plan_id", "occurrence_key", name="uq_task_occurrence"),
        Index("ix_tasks_status_start", "status", "starts_at"),
        Index("ix_tasks_unit_start", "unit_id", "starts_at"),
        CheckConstraint("ends_at > starts_at", name="ck_task_window"),
    )


class AssignmentStatus(str, enum.Enum):
    invited = "invited"
    assigned = "assigned"
    declined = "declined"
    expired = "expired"
    cancelled = "cancelled"


class Assignment(Base):
    __tablename__ = "assignments"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False
    )
    worker_id: Mapped[int] = mapped_column(
        ForeignKey("care_workers.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[AssignmentStatus] = mapped_column(
        Enum(AssignmentStatus, name="assignment_status"),
        nullable=False,
        default=AssignmentStatus.invited,
    )
    invited_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # "auto" for ranking-based offers, "manual" for coordinator assignment.
    created_via: Mapped[str] = mapped_column(String(20), nullable=False, default="auto")
    created_by_coordinator_id: Mapped[int | None] = mapped_column(
        ForeignKey("coordinators.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    task: Mapped[Task] = relationship(back_populates="assignments")

    __table_args__ = (
        # A task has at most one live offer/booking at a time.
        Index(
            "uq_assignment_active_task",
            "task_id",
            unique=True,
            postgresql_where=(
                "status IN ('invited', 'assigned')"
            ),
        ),
        Index("ix_assignment_worker_status", "worker_id", "status"),
        Index("ix_assignment_expires", "status", "expires_at"),
    )


class AssignmentEvent(Base):
    """Immutable audit trail of every assignment/change action."""

    __tablename__ = "assignment_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    assignment_id: Mapped[int | None] = mapped_column(
        ForeignKey("assignments.id", ondelete="SET NULL"), nullable=True
    )
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    worker_id: Mapped[int | None] = mapped_column(
        ForeignKey("care_workers.id", ondelete="SET NULL"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor: Mapped[str] = mapped_column(String(100), nullable=False)
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_assignment_events_task", "task_id", "created_at"),)


class IdempotentRequest(Base):
    """Accepted request keys so retried accept calls never double-book hours."""

    __tablename__ = "idempotent_requests"

    request_key: Mapped[str] = mapped_column(String(200), primary_key=True)
    worker_id: Mapped[int] = mapped_column(ForeignKey("care_workers.id"), nullable=False)
    assignment_id: Mapped[int | None] = mapped_column(
        ForeignKey("assignments.id", ondelete="SET NULL"), nullable=True
    )
    outcome: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
