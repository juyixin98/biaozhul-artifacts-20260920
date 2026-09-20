"""Generated care tasks.

A task is the concrete occurrence of a plan slot on one local date. Tasks are
deduplicated by ``(plan_version_id, slot_id, occurrence_date)``: regenerating
the same version can never create duplicate orders.

All times are stored timezone-aware in UTC; ``local_date`` records the
occurrence date in the plan timezone and is the identity used for
prerequisites.
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.enums import TaskStatus


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint(
            "plan_version_id",
            "slot_id",
            "occurrence_date",
            name="uq_task_occurrence",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(
        ForeignKey("care_plans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    plan_version_id: Mapped[int] = mapped_column(
        ForeignKey("plan_versions.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    slot_id: Mapped[int | None] = mapped_column(
        ForeignKey("weekly_slots.id", ondelete="RESTRICT"), nullable=True
    )
    # Occurrence date in the *plan* timezone.
    occurrence_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    starts_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    ends_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # End of the allowed start window, in UTC.
    latest_start_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[TaskStatus] = mapped_column(
        String(20), nullable=False, default=TaskStatus.PENDING.value, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # Only set for tasks manually moved off their generated slot.
    manually_adjusted: Mapped[bool] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    qualifications: Mapped[list["TaskQualification"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )
    prerequisites: Mapped[list["TaskPrerequisite"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )
    assignments: Mapped[list["Assignment"]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="Assignment.id",
    )


class TaskQualification(Base):
    __tablename__ = "task_qualifications"
    __table_args__ = (
        UniqueConstraint("task_id", "qualification_id", name="uq_task_qualification"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    qualification_id: Mapped[int] = mapped_column(
        ForeignKey("qualifications.id", ondelete="RESTRICT"), nullable=False
    )

    task: Mapped[Task] = relationship(back_populates="qualifications")
    qualification: Mapped[Qualification] = relationship()


class TaskPrerequisite(Base):
    __tablename__ = "task_prerequisites"
    __table_args__ = (
        UniqueConstraint("task_id", "required_task_id", name="uq_task_prerequisite"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    required_task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="RESTRICT"), nullable=False
    )

    task: Mapped[Task] = relationship(
        back_populates="prerequisites", foreign_keys=[task_id]
    )
    required_task: Mapped[Task] = relationship(foreign_keys=[required_task_id])
