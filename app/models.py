"""SQLAlchemy ORM models for the CareForce scheduling backend.

Scope: care-plan task generation, constraint-based assignment, invitation
expiry/rescheduling, coordinator authorization and audit history. No payroll,
volunteer or medical-decision tables live here.
"""
from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class TaskStatus(str, enum.Enum):
    PLANNED = "planned"        # generated, nobody invited yet
    INVITED = "invited"        # one or more pending invitations exist
    ASSIGNED = "assigned"      # a worker accepted and is on the roster
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"    # superseded by a plan revision / coordinator cancel
    UNASSIGNED = "unassigned"  # allocation attempted, no feasible candidate


class InvitationStatus(str, enum.Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    EXPIRED = "expired"
    CANCELLED = "cancelled"  # superseded by another worker accepting / reassignment
    DECLINED = "declined"


class AssignmentStatus(str, enum.Enum):
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"  # manually unassigned or task cancelled; not counted as load


# Statuses that still hold a worker's calendar / weekly-hours capacity.
LOAD_BEARING_STATUSES = (
    AssignmentStatus.ASSIGNED,
    AssignmentStatus.IN_PROGRESS,
    AssignmentStatus.COMPLETED,
)


class Unit(Base):
    __tablename__ = "units"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)


class Coordinator(Base):
    __tablename__ = "coordinators"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    unit_grants: Mapped[list[CoordinatorUnitGrant]] = relationship(
        back_populates="coordinator", cascade="all, delete-orphan"
    )


class CoordinatorUnitGrant(Base):
    __tablename__ = "coordinator_unit_grants"
    __table_args__ = (UniqueConstraint("coordinator_id", "unit_id", name="uq_grant_coordinator_unit"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    coordinator_id: Mapped[int] = mapped_column(ForeignKey("coordinators.id", ondelete="CASCADE"))
    unit_id: Mapped[int] = mapped_column(ForeignKey("units.id", ondelete="CASCADE"))

    coordinator: Mapped[Coordinator] = relationship(back_populates="unit_grants")


class Worker(Base):
    __tablename__ = "workers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    unit_id: Mapped[int] = mapped_column(ForeignKey("units.id"), nullable=False)


class Qualification(Base):
    """A certifiable skill required by plan templates and held by workers."""

    __tablename__ = "qualifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)


class WorkerQualification(Base):
    __tablename__ = "worker_qualifications"
    __table_args__ = (
        UniqueConstraint("worker_id", "qualification_id", name="uq_worker_qualification"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    worker_id: Mapped[int] = mapped_column(ForeignKey("workers.id", ondelete="CASCADE"))
    qualification_id: Mapped[int] = mapped_column(ForeignKey("qualifications.id", ondelete="CASCADE"))
    # Half-open [valid_from, valid_until) in UTC. NULL valid_until => never expires.
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CarePlan(Base):
    """A versioned care plan.

    A revision inserts a new row with ``revision`` incremented and
    ``active`` flipped on the previous row. Generated tasks point at the
    exact revision they belong to.
    """

    __tablename__ = "care_plans"
    __table_args__ = (
        UniqueConstraint("external_id", "revision", name="uq_plan_external_revision"),
        Index("ix_plan_external_active", "external_id", "active"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    external_id: Mapped[str] = mapped_column(String(100), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    client_name: Mapped[str] = mapped_column(String(200), nullable=False)
    unit_id: Mapped[int] = mapped_column(ForeignKey("units.id"), nullable=False)
    # IANA tz name, e.g. "Asia/Shanghai". All local schedule times are interpreted in it.
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Shanghai")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PlanTaskTemplate(Base):
    """One recurring task definition inside a care plan revision.

    ``window_start``/``window_end`` are wall-clock minutes [0, 1440) in the
    plan timezone. The task may start any time within the window and takes
    ``duration_minutes``. ``weekday_mask`` lists ISO weekdays (1=Mon..7=Sun)
    the task recurs on; empty mask means every day.
    """

    __tablename__ = "plan_task_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("care_plans.id", ondelete="CASCADE"))
    code: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    window_start_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    window_end_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    weekday_mask: Mapped[list[int]] = mapped_column(
        # JSON list of ISO weekdays; [] means every day.
        JSON,
        nullable=False,
        default=list,
    )
    qualification_codes: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )

    __table_args__ = (
        UniqueConstraint("plan_id", "code", name="uq_template_plan_code"),
        CheckConstraint(
            "window_start_minute >= 0 AND window_end_minute <= 1440 "
            "AND window_end_minute > window_start_minute AND duration_minutes > 0",
            name="ck_template_window_duration",
        ),
    )


class PlanPrerequisite(Base):
    """Template-level dependency: ``task_code`` needs ``prerequisite_code`` done first."""

    __tablename__ = "plan_prerequisites"
    __table_args__ = (
        UniqueConstraint("plan_id", "task_code", "prerequisite_code", name="uq_plan_prerequisite"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("care_plans.id", ondelete="CASCADE"))
    task_code: Mapped[str] = mapped_column(String(100), nullable=False)
    prerequisite_code: Mapped[str] = mapped_column(String(100), nullable=False)


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        # Idempotency anchor: a plan revision + template on a given service-local
        # date can only ever produce one task row.
        UniqueConstraint(
            "plan_id", "template_id", "scheduled_date", name="uq_task_plan_template_date"
        ),
        Index("ix_tasks_status_start", "status", "scheduled_start"),
        Index("ix_tasks_plan", "plan_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("care_plans.id"), nullable=False)
    template_id: Mapped[int] = mapped_column(ForeignKey("plan_task_templates.id"), nullable=False)
    unit_id: Mapped[int] = mapped_column(ForeignKey("units.id"), nullable=False)
    # Service-local date the occurrence belongs to (YYYY-MM-DD in plan tz).
    scheduled_date: Mapped[str] = mapped_column(String(10), nullable=False)
    # Concrete occurrence interval in UTC (window_start of that local date + duration).
    scheduled_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    scheduled_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=TaskStatus.PLANNED.value)
    # Bumped whenever a new invitation search cycle starts. Invitations carry
    # the generation they belong to, so workers expired in a previous cycle
    # are skipped until everyone feasible has had a turn (then a new cycle
    # begins instead of re-inviting the same worker forever).
    allocation_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    qualification_codes: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )
    prerequisites: Mapped[list[dict]] = mapped_column(
        # [{"task_id": int, "template_code": str}] resolved at generation time.
        JSON,
        nullable=False,
        default=list,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    invitations: Mapped[list[Invitation]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )
    assignments: Mapped[list[Assignment]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )


class Invitation(Base):
    __tablename__ = "invitations"
    __table_args__ = (
        # At most one live invitation per task/worker (a worker may be invited
        # again in a later round after expiry).
        UniqueConstraint("task_id", "worker_id", "round", name="uq_invitation_task_worker_round"),
        Index("ix_invitations_status_expires", "status", "expires_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    worker_id: Mapped[int] = mapped_column(ForeignKey("workers.id", ondelete="CASCADE"))
    round: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # Search-cycle generation copied from the task when the invitation was made.
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=InvitationStatus.PENDING.value)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    task: Mapped[Task] = relationship(back_populates="invitations")


class Assignment(Base):
    """An accepted invitation — the single source of truth for worker load."""

    __tablename__ = "assignments"
    __table_args__ = (
        # Hard backstop: a task can only carry one non-cancelled assignment,
        # so two workers racing to accept cannot both land on the roster.
        UniqueConstraint("task_id", name="uq_assignment_task"),
        Index("ix_assignments_worker", "worker_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    worker_id: Mapped[int] = mapped_column(ForeignKey("workers.id", ondelete="CASCADE"))
    invitation_id: Mapped[int] = mapped_column(ForeignKey("invitations.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=AssignmentStatus.ASSIGNED.value)
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    task: Mapped[Task] = relationship(back_populates="assignments")


class AuditLog(Base):
    """Every coordinator/system action that changes scheduling state."""

    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_task", "task_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True)
    actor_type: Mapped[str] = mapped_column(String(20), nullable=False)  # "coordinator" | "system"
    actor_id: Mapped[str] = mapped_column(String(50), nullable=False)
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Before/after snapshot for change history.
    detail: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
