"""Care plans and their versioned snapshots.

A :class:`CarePlan` groups every revision of the same care arrangement. Every
revision creates a new immutable :class:`PlanVersion` together with fresh
:class:`WeeklySlot`, :class:`PlanQualification` and :class:`PlanPrerequisite`
rows. Task generation always expands the *active* version.

Revision semantics (domain rule): a revision only affects tasks that have not
started. Existing accepted/in-progress/completed tasks stay on their old
version; generated-but-unassigned tasks are cancelled and regenerated.
"""
from __future__ import annotations

from datetime import time

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Time,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.enums import PlanVersionStatus


class CarePlan(Base):
    __tablename__ = "care_plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    unit_id: Mapped[int] = mapped_column(
        ForeignKey("units.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # Human-friendly identifier, e.g. the care recipient's initials.
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped["DateTime"] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    versions: Mapped[list["PlanVersion"]] = relationship(
        back_populates="plan",
        cascade="all, delete-orphan",
        order_by="PlanVersion.version_number",
    )

    def active_version(self) -> "PlanVersion | None":
        for version in self.versions:
            if version.status == PlanVersionStatus.ACTIVE:
                return version
        return None


class PlanVersion(Base):
    __tablename__ = "plan_versions"
    __table_args__ = (
        UniqueConstraint("plan_id", "version_number", name="uq_plan_version"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(
        ForeignKey("care_plans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[PlanVersionStatus] = mapped_column(
        String(20), nullable=False, default=PlanVersionStatus.DRAFT.value
    )
    # All scheduling for the plan happens in this timezone.
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    # Recurrence period in days: 1 = daily, 7 = weekly, 14 = every two weeks.
    period_days: Mapped[int] = mapped_column(Integer, nullable=False, default=7)
    # Date (in plan tz) of the first occurrence this version applies to. The
    # period is measured from this anchor, which keeps regeneration
    # idempotent: the same (version, occurrence_date) always maps to one task.
    anchor_date: Mapped["DateTime"] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped["DateTime"] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    change_note: Mapped[str | None] = mapped_column(String(500), nullable=True)

    plan: Mapped[CarePlan] = relationship(back_populates="versions")
    slots: Mapped[list["WeeklySlot"]] = relationship(
        back_populates="version",
        cascade="all, delete-orphan",
    )
    qualifications: Mapped[list["PlanQualification"]] = relationship(
        back_populates="version",
        cascade="all, delete-orphan",
    )
    prerequisites: Mapped[list["PlanPrerequisite"]] = relationship(
        back_populates="version",
        cascade="all, delete-orphan",
    )


class WeeklySlot(Base):
    """One recurring clock-time inside a plan version.

    ``weekday`` follows Python's ``date.weekday()``: Monday=0 ... Sunday=6.
    ``latest_start_at`` is the end of the allowed window (same day, local
    time). The generated task starts at ``start_at``; its duration is fixed at
    ``duration_minutes``.
    """

    __tablename__ = "weekly_slots"
    __table_args__ = (
        UniqueConstraint(
            "plan_version_id",
            "weekday",
            "start_at",
            name="uq_weekly_slot",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_version_id: Mapped[int] = mapped_column(
        ForeignKey("plan_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    weekday: Mapped[int] = mapped_column(Integer, nullable=False)
    start_at: Mapped[time] = mapped_column(Time(timezone=False), nullable=False)
    latest_start_at: Mapped[time] = mapped_column(Time(timezone=False), nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)

    version: Mapped[PlanVersion] = relationship(back_populates="slots")


class PlanQualification(Base):
    __tablename__ = "plan_qualifications"
    __table_args__ = (
        UniqueConstraint(
            "plan_version_id", "qualification_id", name="uq_plan_qualification"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_version_id: Mapped[int] = mapped_column(
        ForeignKey("plan_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    qualification_id: Mapped[int] = mapped_column(
        ForeignKey("qualifications.id", ondelete="RESTRICT"), nullable=False
    )

    version: Mapped[PlanVersion] = relationship(back_populates="qualifications")
    qualification: Mapped[Qualification] = relationship()


class PlanPrerequisite(Base):
    """A care plan whose *completed* task must exist before this version's
    task may be assigned.

    ``offset_minutes`` describes how the prerequisite task relates in time
    (used only for display/sorting); the enforcement rule is simply that the
    prerequisite task for the same occurrence date is completed.
    """

    __tablename__ = "plan_prerequisites"
    __table_args__ = (
        UniqueConstraint(
            "plan_version_id", "required_plan_id", name="uq_plan_prerequisite"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_version_id: Mapped[int] = mapped_column(
        ForeignKey("plan_versions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    required_plan_id: Mapped[int] = mapped_column(
        ForeignKey("care_plans.id", ondelete="RESTRICT"), nullable=False
    )
    offset_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    version: Mapped[PlanVersion] = relationship(back_populates="prerequisites")
    required_plan: Mapped[CarePlan] = relationship(foreign_keys=[required_plan_id])
