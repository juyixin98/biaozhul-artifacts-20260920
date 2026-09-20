"""Assignments (invitations) and the audit trail.

At most one *live* assignment (PENDING invitation or ACCEPTED) may exist on a
task at any time. The partial unique indexes below enforce that at the
database level, which is what makes concurrent accepts safe (see
``app/services/scheduling.py``).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.enums import AssignmentStatus, EventType


class Assignment(Base):
    __tablename__ = "assignments"
    __table_args__ = (
        # At most one open invitation (pending) per task.
        Index(
            "uq_one_pending_per_task",
            "task_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
            sqlite_where=text("status = 'pending'"),
        ),
        # At most one accepted assignment per task.
        Index(
            "uq_one_accepted_per_task",
            "task_id",
            unique=True,
            postgresql_where=text("status = 'accepted'"),
            sqlite_where=text("status = 'accepted'"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    worker_id: Mapped[int] = mapped_column(
        ForeignKey("workers.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status: Mapped[AssignmentStatus] = mapped_column(
        String(20), nullable=False, default=AssignmentStatus.PENDING.value, index=True
    )
    invited_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    responded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Snapshot of the task interval at invitation time, so constraint checks
    # and the audit trail stay meaningful even if the task row later changes.
    task_starts_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    task_ends_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # "system" for automatic scheduling; coordinator external_id for manual.
    invited_by: Mapped[str] = mapped_column(String(200), nullable=False, default="system")

    task: Mapped["Task"] = relationship(back_populates="assignments")
    worker: Mapped["Worker"] = relationship()


class AssignmentEvent(Base):
    """Append-only audit log for every scheduling decision."""

    __tablename__ = "assignment_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    assignment_id: Mapped[int | None] = mapped_column(
        ForeignKey("assignments.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    worker_id: Mapped[int | None] = mapped_column(
        ForeignKey("workers.id", ondelete="SET NULL"), nullable=True
    )
    coordinator_id: Mapped[int | None] = mapped_column(
        ForeignKey("coordinators.id", ondelete="SET NULL"), nullable=True
    )
    event_type: Mapped[EventType] = mapped_column(String(40), nullable=False)
    # Structured detail (violated constraints, reason, payload) as JSON text.
    detail: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
