"""SQLAlchemy ORM models.

Versioning model
----------------
* ``Program`` carries mutable offering data (capacity, deadline) and points at
  the currently active published version.
* ``ProgramVersion`` rows are append-only once published: a published version's
  steps and prerequisites never change.  Editing a program creates a new draft;
  publishing the draft freezes it as a new immutable version.  Rolling back
  simply repoints ``current_version_id`` at an older published version.
* Enrolments are pinned to ``version_id`` at enrolment time, so neither a new
  publish nor a rollback can alter a learner's learning content.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    DateTime,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.statuses import VERSION_DRAFT, ENR_PENDING


def utcnow() -> datetime:
    # Local import: models are imported before the clock is exercised in tests.
    from app.clock import now_utc

    return now_utc()


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    login: Mapped[str] = mapped_column(String(80), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)  # supervisor | learner

    sessions: Mapped[list["AuthSession"]] = relationship(back_populates="user")


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped[User] = relationship(back_populates="sessions")


class Program(Base):
    __tablename__ = "programs"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    supervisor_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    capacity: Mapped[int] = mapped_column(Integer, nullable=False)
    enrollment_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    current_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("program_versions.id", use_alter=True, name="fk_program_current_version"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        CheckConstraint("capacity >= 0", name="ck_program_capacity_nonneg"),
    )

    versions: Mapped[list["ProgramVersion"]] = relationship(
        back_populates="program",
        foreign_keys="ProgramVersion.program_id",
    )


class ProgramVersion(Base):
    __tablename__ = "program_versions"
    __table_args__ = (
        UniqueConstraint("program_id", "version_number", name="uq_version_number"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    program_id: Mapped[int] = mapped_column(ForeignKey("programs.id"), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=VERSION_DRAFT)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    program: Mapped[Program] = relationship(
        back_populates="versions", foreign_keys=[program_id]
    )
    steps: Mapped[list["Step"]] = relationship(
        back_populates="version", cascade="all, delete-orphan", order_by="Step.position"
    )


class Step(Base):
    __tablename__ = "steps"
    __table_args__ = (
        UniqueConstraint("version_id", "position", name="uq_step_position"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    version_id: Mapped[int] = mapped_column(
        ForeignKey("program_versions.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    instruction: Mapped[str] = mapped_column(Text, nullable=False)
    pass_condition: Mapped[str] = mapped_column(Text, nullable=False)

    version: Mapped[ProgramVersion] = relationship(back_populates="steps")
    prerequisites: Mapped[list["StepPrerequisite"]] = relationship(
        back_populates="step",
        cascade="all, delete-orphan",
        primaryjoin="Step.id == StepPrerequisite.step_id",
    )


class StepPrerequisite(Base):
    __tablename__ = "step_prerequisites"
    __table_args__ = (
        UniqueConstraint("step_id", "prerequisite_id", name="uq_step_prereq"),
        CheckConstraint("step_id <> prerequisite_id", name="ck_prereq_not_self"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    step_id: Mapped[int] = mapped_column(ForeignKey("steps.id", ondelete="CASCADE"), nullable=False)
    prerequisite_id: Mapped[int] = mapped_column(
        ForeignKey("steps.id", ondelete="CASCADE"), nullable=False
    )

    step: Mapped[Step] = relationship(foreign_keys=[step_id])


class Enrollment(Base):
    __tablename__ = "enrollments"

    id: Mapped[int] = mapped_column(primary_key=True)
    program_id: Mapped[int] = mapped_column(ForeignKey("programs.id"), nullable=False)
    version_id: Mapped[int] = mapped_column(
        ForeignKey("program_versions.id", ondelete="RESTRICT"), nullable=False
    )
    learner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=ENR_PENDING)
    waitlist_position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    seat_granted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status_changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        # A learner only ever has one live enrolment per program; cancelled /
        # expired attempts stay as history and may be followed by a new attempt.
        Index(
            "uq_enrolment_active_learner",
            "program_id",
            "learner_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'confirmed', 'waitlisted')"),
        ),
    )


class StepResult(Base):
    __tablename__ = "step_results"
    __table_args__ = (
        # One result per (enrolment, step): repeat submissions are idempotent
        # and can never be double counted.
        UniqueConstraint("enrollment_id", "step_id", name="uq_result_enrolment_step"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    enrollment_id: Mapped[int] = mapped_column(
        ForeignKey("enrollments.id", ondelete="CASCADE"), nullable=False
    )
    step_id: Mapped[int] = mapped_column(ForeignKey("steps.id", ondelete="RESTRICT"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, default="")
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_correction_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_corrected_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)


class ResultCorrection(Base):
    """Audit row for every supervisor correction of a step result."""

    __tablename__ = "result_corrections"

    id: Mapped[int] = mapped_column(primary_key=True)
    result_id: Mapped[int] = mapped_column(
        ForeignKey("step_results.id", ondelete="CASCADE"), nullable=False
    )
    supervisor_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    from_status: Mapped[str] = mapped_column(String(20), nullable=False)
    to_status: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Certificate(Base):
    """At most one certificate per enrolment, tied to the pinned version.

    A correction that invalidates completion flips ``revoked`` (and records the
    reason on the correction); a correction back restores the same row instead
    of minting a new serial, so exactly one valid certificate ever exists.
    """

    __tablename__ = "certificates"
    __table_args__ = (
        UniqueConstraint("enrollment_id", name="uq_certificate_enrolment"),
        UniqueConstraint("serial_number", name="uq_certificate_serial"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    enrollment_id: Mapped[int] = mapped_column(
        ForeignKey("enrollments.id", ondelete="CASCADE"), nullable=False
    )
    version_id: Mapped[int] = mapped_column(ForeignKey("program_versions.id"), nullable=False)
    serial_number: Mapped[str] = mapped_column(String(40), nullable=False, unique=True)
    content_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    revoked: Mapped[bool] = mapped_column(default=False, nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
