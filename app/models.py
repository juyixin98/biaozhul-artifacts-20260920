"""SQLAlchemy ORM models for SkillPulse."""
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

# ---------------------------------------------------------------------------
# Enumerations (kept as plain strings + CHECK constraints; easy to evolve
# and portable across PostgreSQL/SQLite).
# ---------------------------------------------------------------------------

ROLE_SUPERVISOR = "supervisor"
ROLE_LEARNER = "learner"

VERSION_DRAFT = "draft"
VERSION_PUBLISHED = "published"

ENROLLMENT_WAITLISTED = "waitlisted"
ENROLLMENT_PENDING = "pending_confirmation"
ENROLLMENT_CONFIRMED = "confirmed"
ENROLLMENT_CANCELLED = "cancelled"
ENROLLMENT_EXPIRED = "expired"

RESULT_PASSED = "passed"
RESULT_FAILED = "failed"

CERT_VALID = "valid"
CERT_INVALID = "invalid"


class AppClock(Base):
    """Singleton row (id=1) storing the controllable clock offset."""

    __tablename__ = "app_clock"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    offset_seconds: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default="now()"
    )

    __table_args__ = (
        CheckConstraint(
            f"role IN ('{ROLE_SUPERVISOR}', '{ROLE_LEARNER}')",
            name="ck_users_role",
        ),
    )

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "role": self.role}


class Program(Base):
    """A training program owned by a supervisor. Versions are immutable
    once published; ``current_version_id`` selects the active rollout."""

    __tablename__ = "programs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    current_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("program_versions.id", use_alter=True, name="fk_program_current_version"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default="now()"
    )

    versions: Mapped[list["ProgramVersion"]] = relationship(
        back_populates="program",
        foreign_keys="ProgramVersion.program_id",
        order_by="ProgramVersion.version_number",
    )
    current_version: Mapped["ProgramVersion | None"] = relationship(
        foreign_keys=[current_version_id], post_update=True
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "owner_id": self.owner_id,
            "current_version_id": self.current_version_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class ProgramVersion(Base):
    __tablename__ = "program_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    program_id: Mapped[int] = mapped_column(
        ForeignKey("programs.id", ondelete="RESTRICT"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    content_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default="now()"
    )
    published_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "program_id", "version_number", name="uq_version_program_number"
        ),
        CheckConstraint(
            f"status IN ('{VERSION_DRAFT}', '{VERSION_PUBLISHED}')",
            name="ck_version_status",
        ),
    )

    program: Mapped[Program] = relationship(
        back_populates="versions", foreign_keys=[program_id]
    )
    steps: Mapped[list["Step"]] = relationship(
        back_populates="version",
        order_by="Step.position",
        cascade="all, delete-orphan",
    )

    def to_dict(self, include_steps: bool = False) -> dict:
        data = {
            "id": self.id,
            "program_id": self.program_id,
            "version_number": self.version_number,
            "status": self.status,
            "content_digest": self.content_digest,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "published_at": self.published_at.isoformat()
            if self.published_at
            else None,
        }
        if include_steps:
            data["steps"] = [s.to_dict() for s in self.steps]
        return data


class Step(Base):
    __tablename__ = "steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version_id: Mapped[int] = mapped_column(
        ForeignKey("program_versions.id", ondelete="CASCADE"), nullable=False
    )
    # Stable client-supplied key within a version; prerequisites reference it.
    key: Mapped[str] = mapped_column(String(60), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    instruction: Mapped[str] = mapped_column(Text, nullable=False)
    pass_condition: Mapped[str] = mapped_column(Text, nullable=False)

    __table_args__ = (
        UniqueConstraint("version_id", "key", name="uq_step_version_key"),
        UniqueConstraint("version_id", "position", name="uq_step_version_position"),
        CheckConstraint("position >= 1", name="ck_step_position"),
    )

    version: Mapped[ProgramVersion] = relationship(back_populates="steps")
    prerequisites: Mapped[list["StepPrerequisite"]] = relationship(
        back_populates="step",
        cascade="all, delete-orphan",
        foreign_keys="StepPrerequisite.step_id",
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "key": self.key,
            "position": self.position,
            "instruction": self.instruction,
            "pass_condition": self.pass_condition,
            "prerequisite_keys": [p.prerequisite_key for p in self.prerequisites],
        }


class StepPrerequisite(Base):
    __tablename__ = "step_prerequisites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    step_id: Mapped[int] = mapped_column(
        ForeignKey("steps.id", ondelete="CASCADE"), nullable=False
    )
    prerequisite_key: Mapped[str] = mapped_column(String(60), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "step_id", "prerequisite_key", name="uq_prereq_step_key"
        ),
    )

    step: Mapped[Step] = relationship(
        back_populates="prerequisites", foreign_keys=[step_id]
    )


class Course(Base):
    """A scheduled offering bound to an *immutable published* program
    version. Capacity / waitlist live here."""

    __tablename__ = "courses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    version_id: Mapped[int] = mapped_column(
        ForeignKey("program_versions.id", ondelete="RESTRICT"), nullable=False
    )
    capacity: Mapped[int] = mapped_column(Integer, nullable=False)
    enroll_deadline: Mapped[datetime] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default="now()"
    )

    __table_args__ = (CheckConstraint("capacity >= 0", name="ck_course_capacity"),)

    version: Mapped[ProgramVersion] = relationship()

    def to_dict(self, extra: dict | None = None) -> dict:
        data = {
            "id": self.id,
            "title": self.title,
            "version_id": self.version_id,
            "capacity": self.capacity,
            "enroll_deadline": self.enroll_deadline.isoformat(),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
        if extra:
            data.update(extra)
        return data


class Enrollment(Base):
    __tablename__ = "enrollments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_id: Mapped[int] = mapped_column(
        ForeignKey("courses.id", ondelete="CASCADE"), nullable=False
    )
    learner_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    version_id: Mapped[int] = mapped_column(
        ForeignKey("program_versions.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    waitlist_position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    offered_at: Mapped[datetime | None] = mapped_column(nullable=True)
    seat_expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default="now()"
    )

    __table_args__ = (
        UniqueConstraint(
            "course_id",
            "learner_id",
            name="uq_enrollment_course_learner",
        ),
        Index("ix_enrollments_course_status", "course_id", "status"),
        CheckConstraint(
            f"status IN ('{ENROLLMENT_WAITLISTED}', '{ENROLLMENT_PENDING}', "
            f"'{ENROLLMENT_CONFIRMED}', '{ENROLLMENT_CANCELLED}', "
            f"'{ENROLLMENT_EXPIRED}')",
            name="ck_enrollment_status",
        ),
    )

    results: Mapped[list["StepResult"]] = relationship(
        back_populates="enrollment", cascade="all, delete-orphan"
    )
    certificate: Mapped["Certificate | None"] = relationship(
        back_populates="enrollment", uselist=False, cascade="all, delete-orphan"
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "course_id": self.course_id,
            "learner_id": self.learner_id,
            "version_id": self.version_id,
            "status": self.status,
            "waitlist_position": self.waitlist_position,
            "offered_at": self.offered_at.isoformat() if self.offered_at else None,
            "seat_expires_at": (
                self.seat_expires_at.isoformat() if self.seat_expires_at else None
            ),
            "confirmed_at": (
                self.confirmed_at.isoformat() if self.confirmed_at else None
            ),
            "cancelled_at": (
                self.cancelled_at.isoformat() if self.cancelled_at else None
            ),
            "completed_at": (
                self.completed_at.isoformat() if self.completed_at else None
            ),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class StepResult(Base):
    __tablename__ = "step_results"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    enrollment_id: Mapped[int] = mapped_column(
        ForeignKey("enrollments.id", ondelete="CASCADE"), nullable=False
    )
    step_id: Mapped[int] = mapped_column(
        ForeignKey("steps.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    submission_content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_submitted_at: Mapped[datetime | None] = mapped_column(nullable=True)
    reviewed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    # True when the current status was set by a supervisor correction
    # rather than the normal review flow.
    corrected: Mapped[bool] = mapped_column(default=False, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "enrollment_id", "step_id", name="uq_result_enrollment_step"
        ),
        CheckConstraint(
            f"status IN ('{RESULT_PASSED}', '{RESULT_FAILED}')",
            name="ck_result_status",
        ),
    )

    enrollment: Mapped[Enrollment] = relationship(back_populates="results")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "enrollment_id": self.enrollment_id,
            "step_id": self.step_id,
            "status": self.status,
            "submission_content": self.submission_content,
            "attempts": self.attempts,
            "last_submitted_at": (
                self.last_submitted_at.isoformat()
                if self.last_submitted_at
                else None
            ),
            "reviewed_by": self.reviewed_by,
            "reviewed_at": self.reviewed_at.isoformat()
            if self.reviewed_at
            else None,
            "corrected": self.corrected,
        }


class ResultCorrection(Base):
    """Audit record for every supervisor override of a step result."""

    __tablename__ = "result_corrections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    result_id: Mapped[int] = mapped_column(
        ForeignKey("step_results.id", ondelete="CASCADE"), nullable=False
    )
    supervisor_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    previous_status: Mapped[str] = mapped_column(String(20), nullable=False)
    new_status: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default="now()"
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "result_id": self.result_id,
            "supervisor_id": self.supervisor_id,
            "previous_status": self.previous_status,
            "new_status": self.new_status,
            "reason": self.reason,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Certificate(Base):
    """One row per enrolment. Issued once (unique serial number + content
    digest); its ``status`` flips to invalid if the underlying results
    change, and back to valid if completion is restored — the row and
    serial are never reused or duplicated."""

    __tablename__ = "certificates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    enrollment_id: Mapped[int] = mapped_column(
        ForeignKey("enrollments.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    serial_number: Mapped[str] = mapped_column(String(60), nullable=False, unique=True)
    version_id: Mapped[int] = mapped_column(
        ForeignKey("program_versions.id", ondelete="RESTRICT"), nullable=False
    )
    content_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    issued_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default="now()"
    )

    __table_args__ = (
        CheckConstraint(
            f"status IN ('{CERT_VALID}', '{CERT_INVALID}')",
            name="ck_certificate_status",
        ),
    )

    enrollment: Mapped[Enrollment] = relationship(back_populates="certificate")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "enrollment_id": self.enrollment_id,
            "serial_number": self.serial_number,
            "version_id": self.version_id,
            "content_digest": self.content_digest,
            "status": self.status,
            "issued_at": self.issued_at.isoformat() if self.issued_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
