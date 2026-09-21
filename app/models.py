from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from . import clock


class Base(DeclarativeBase):
    pass


# Enrollment statuses
ENROLLMENT_PENDING = "pending"          # holds a seat, awaiting confirmation
ENROLLMENT_CONFIRMED = "confirmed"      # seat confirmed, learning in progress
ENROLLMENT_WAITLISTED = "waitlisted"    # queued, no seat held
ENROLLMENT_CANCELLED = "cancelled"
ENROLLMENT_EXPIRED = "expired"          # seat hold expired after 48h

ACTIVE_ENROLLMENT_STATUSES = (
    ENROLLMENT_PENDING,
    ENROLLMENT_CONFIRMED,
    ENROLLMENT_WAITLISTED,
)

# Program version statuses
VERSION_DRAFT = "draft"
VERSION_PUBLISHED = "published"
VERSION_ARCHIVED = "archived"

# Certificate statuses
CERT_VALID = "valid"
CERT_REVOKED = "revoked"


class Program(Base):
    __tablename__ = "programs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(100))
    # Pointer to the published version new enrollments bind to by default.
    # Deliberately not a FK: existing enrollments keep their own version_id.
    current_version_id: Mapped[Optional[uuid.UUID]] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=clock.now
    )

    versions: Mapped[list["ProgramVersion"]] = relationship(
        back_populates="program", order_by="ProgramVersion.version_number"
    )


class ProgramVersion(Base):
    __tablename__ = "program_versions"
    __table_args__ = (UniqueConstraint("program_id", "version_number"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    program_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("programs.id"), index=True
    )
    version_number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default=VERSION_DRAFT)
    change_note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=clock.now
    )
    published_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    program: Mapped[Program] = relationship(back_populates="versions")
    steps: Mapped[list["Step"]] = relationship(
        back_populates="version",
        order_by="Step.order_index",
        cascade="all, delete-orphan",
    )


class Step(Base):
    __tablename__ = "steps"
    __table_args__ = (UniqueConstraint("version_id", "step_key"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("program_versions.id"), index=True
    )
    step_key: Mapped[str] = mapped_column(String(40))
    order_index: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(200))
    instructions: Mapped[str] = mapped_column(Text, default="")
    pass_condition: Mapped[str] = mapped_column(Text, default="")
    # List of step_keys that must be passed before this step can be submitted.
    prerequisites: Mapped[list] = mapped_column(JSONB, default=list)

    version: Mapped[ProgramVersion] = relationship(back_populates="steps")


class Course(Base):
    __tablename__ = "courses"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    program_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("programs.id"), index=True
    )
    title: Mapped[str] = mapped_column(String(200))
    capacity: Mapped[int] = mapped_column(Integer)
    # Seats currently held (pending) or occupied (confirmed). Waitlisted
    # enrollments do not hold seats. Updated under a row lock on the course.
    seats_taken: Mapped[int] = mapped_column(Integer, default=0)
    enrollment_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=clock.now
    )

    program: Mapped[Program] = relationship()


class Enrollment(Base):
    __tablename__ = "enrollments"
    __table_args__ = (UniqueConstraint("course_id", "learner_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    course_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("courses.id"), index=True
    )
    learner_id: Mapped[str] = mapped_column(String(100), index=True)
    # The exact program version this learner studies. Never rewritten by
    # later publishes or rollbacks.
    version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("program_versions.id")
    )
    status: Mapped[str] = mapped_column(String(20), default=ENROLLMENT_PENDING)
    # Deadline to confirm the held seat (created_at + 48h when pending).
    confirm_deadline: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Queue position for the waitlist is (created_at, id).
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=clock.now
    )

    course: Mapped[Course] = relationship()
    version: Mapped[ProgramVersion] = relationship()
    results: Mapped[list["StepResult"]] = relationship(back_populates="enrollment")


class StepResult(Base):
    __tablename__ = "step_results"
    __table_args__ = (UniqueConstraint("enrollment_id", "step_key"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    enrollment_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("enrollments.id"), index=True
    )
    step_key: Mapped[str] = mapped_column(String(40))
    passed: Mapped[bool] = mapped_column(Boolean)
    # "learner" for the original submission, "correction" after a supervisor fix.
    source: Mapped[str] = mapped_column(String(20), default="learner")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=clock.now
    )

    enrollment: Mapped[Enrollment] = relationship(back_populates="results")
    corrections: Mapped[list["Correction"]] = relationship(back_populates="result")


class Correction(Base):
    __tablename__ = "corrections"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    result_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("step_results.id"), index=True
    )
    passed: Mapped[bool] = mapped_column(Boolean)
    reason: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=clock.now
    )

    result: Mapped[StepResult] = relationship(back_populates="corrections")


class Certificate(Base):
    __tablename__ = "certificates"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # One certificate per enrollment, ever.
    enrollment_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("enrollments.id"), unique=True
    )
    version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("program_versions.id")
    )
    serial_number: Mapped[str] = mapped_column(String(40), unique=True)
    content_digest: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default=CERT_VALID)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=clock.now
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    enrollment: Mapped[Enrollment] = relationship()
