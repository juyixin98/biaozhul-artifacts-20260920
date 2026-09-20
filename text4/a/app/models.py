from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class UserRole(str, enum.Enum):
    MANAGER = "MANAGER"
    LEARNER = "LEARNER"


class VersionStatus(str, enum.Enum):
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"


class EnrollmentStatus(str, enum.Enum):
    ENROLLED = "ENROLLED"        # holds a seat, must confirm within 48h
    CONFIRMED = "CONFIRMED"      # seat confirmed
    WAITLISTED = "WAITLISTED"    # in the waiting queue
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"          # 48h hold lapsed


class ResultStatus(str, enum.Enum):
    PASSED = "PASSED"
    FAILED = "FAILED"


class CertificateStatus(str, enum.Enum):
    VALID = "VALID"
    REVOKED = "REVOKED"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(Enum(UserRole, name="user_role"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Program(Base):
    __tablename__ = "programs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    # Pointer used for *new* enrollments. Existing enrollments keep their pin.
    # use_alter breaks the programs <-> program_versions FK cycle at DDL time.
    current_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("program_versions.id", use_alter=True, name="fk_program_current_version"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    current_version: Mapped["ProgramVersion | None"] = relationship(
        foreign_keys=[current_version_id]
    )


class ProgramVersion(Base):
    __tablename__ = "program_versions"
    __table_args__ = (
        UniqueConstraint("program_id", "version", name="ux_version_program_version"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    program_id: Mapped[int] = mapped_column(ForeignKey("programs.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[VersionStatus] = mapped_column(
        Enum(VersionStatus, name="version_status"), nullable=False
    )
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    steps: Mapped[list["Step"]] = relationship(
        back_populates="program_version", cascade="all, delete-orphan", order_by="Step.order_index"
    )


class Step(Base):
    __tablename__ = "steps"
    __table_args__ = (
        UniqueConstraint("version_id", "order_index", name="ux_step_version_order"),
        CheckConstraint("order_index BETWEEN 1 AND 50", name="ck_step_order_range"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("program_versions.id"), nullable=False)
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    pass_criteria: Mapped[str] = mapped_column(Text, nullable=False)

    program_version: Mapped[ProgramVersion] = relationship(back_populates="steps")


class StepDependency(Base):
    __tablename__ = "step_dependencies"
    __table_args__ = (
        UniqueConstraint("step_id", "prerequisite_step_id", name="ux_dep_pair"),
        CheckConstraint("step_id <> prerequisite_step_id", name="ck_dep_no_self"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("program_versions.id"), nullable=False)
    step_id: Mapped[int] = mapped_column(ForeignKey("steps.id"), nullable=False)
    prerequisite_step_id: Mapped[int] = mapped_column(ForeignKey("steps.id"), nullable=False)


class Course(Base):
    """A concrete offering of one immutable program version."""

    __tablename__ = "courses"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    version_id: Mapped[int] = mapped_column(
        ForeignKey("program_versions.id"), unique=True, nullable=False
    )
    capacity: Mapped[int] = mapped_column(Integer, nullable=False)
    enrollment_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (CheckConstraint("capacity > 0", name="ck_course_capacity_positive"),)


class Enrollment(Base):
    __tablename__ = "enrollments"
    __table_args__ = (
        # A learner only ever has one row per pinned version -> idempotent sign-up.
        UniqueConstraint("learner_id", "version_id", name="ux_enrollment_learner_version"),
        # Backstop for the capacity check: seat numbers 1..capacity are unique.
        UniqueConstraint("version_id", "seat_number", name="ux_enrollment_seat_number"),
        Index("ix_enrollment_version_status", "version_id", "status"),
        Index("ix_enrollment_waitlist", "version_id", "waitlist_position"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    learner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    version_id: Mapped[int] = mapped_column(ForeignKey("program_versions.id"), nullable=False)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"), nullable=False)
    status: Mapped[EnrollmentStatus] = mapped_column(
        Enum(EnrollmentStatus, name="enrollment_status"), nullable=False
    )
    # Seat slot inside the course capacity; NULL when not holding one.
    seat_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    waitlist_position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    seat_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class StepResult(Base):
    __tablename__ = "step_results"
    __table_args__ = (
        UniqueConstraint("enrollment_id", "step_id", name="ux_result_enrollment_step"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    enrollment_id: Mapped[int] = mapped_column(ForeignKey("enrollments.id"), nullable=False)
    step_id: Mapped[int] = mapped_column(ForeignKey("steps.id"), nullable=False)
    status: Mapped[ResultStatus] = mapped_column(
        Enum(ResultStatus, name="result_status"), nullable=False
    )
    latest_submission: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Fingerprint of the last *counted* learner submission; exact repeats are
    # idempotent and do not raise attempt_count. Cleared by manager correction
    # so the same content may be resubmitted afterwards.
    submission_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    passed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    corrected_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    correction_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    corrected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Certificate(Base):
    __tablename__ = "certificates"
    __table_args__ = (
        UniqueConstraint("serial", name="ux_certificate_serial"),
        # Exactly one certificate row per enrollment. It flips between VALID
        # and REVOKED as completion changes; retries can never mint a second
        # row, and re-completion restores the same certificate instead of
        # issuing a new serial.
        UniqueConstraint("enrollment_id", name="ux_certificate_enrollment"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    enrollment_id: Mapped[int] = mapped_column(ForeignKey("enrollments.id"), nullable=False)
    version_id: Mapped[int] = mapped_column(ForeignKey("program_versions.id"), nullable=False)
    serial: Mapped[str] = mapped_column(String(64), nullable=False)
    learner_name: Mapped[str] = mapped_column(String(255), nullable=False)
    program_title: Mapped[str] = mapped_column(String(255), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[CertificateStatus] = mapped_column(
        Enum(CertificateStatus, name="certificate_status"), nullable=False
    )
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoke_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class ClockOverride(Base):
    """Single-row table holding the virtual clock used by tests and demos."""

    __tablename__ = "clock_overrides"

    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    virtual_now: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
