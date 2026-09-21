"""Database models: architectures, datasets, jobs, events, checkpoints."""
from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return uuid.uuid4().hex


class JobStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class Architecture(Base):
    """An immutable published network definition (a versioned artifact)."""
    __tablename__ = "architectures"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    spec_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    in_features: Mapped[int] = mapped_column(Integer, nullable=False)
    out_features: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                default=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("name", "version", name="uq_arch_name_version"),
    )


class Dataset(Base):
    """A registered dataset file inside the whitelist plus its digest."""
    __tablename__ = "datasets"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    feature_path: Mapped[str] = mapped_column(Text, nullable=False)
    target_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    task: Mapped[str] = mapped_column(String(32), nullable=False)
    summary_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                default=utcnow, nullable=False)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(200), nullable=False)
    architecture_id: Mapped[str] = mapped_column(
        ForeignKey("architectures.id"), nullable=False)
    dataset_id: Mapped[str] = mapped_column(
        ForeignKey("datasets.id"), nullable=False)

    # Immutable training contract bound at creation time.
    hyperparams_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    seed: Mapped[int] = mapped_column(Integer, nullable=False)
    dataset_summary_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    # Fixed sample indices for the train/validation split.
    train_idx_json: Mapped[list] = mapped_column(JSON, nullable=False)
    val_idx_json: Mapped[list] = mapped_column(JSON, nullable=False)

    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus), nullable=False, default=JobStatus.QUEUED)
    epochs_total: Mapped[int] = mapped_column(Integer, nullable=False)
    epochs_done: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Leasing.
    executor_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)

    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)

    events: Mapped[list["Event"]] = relationship(
        back_populates="job", cascade="all, delete-orphan",
        order_by="Event.seq")
    checkpoints: Mapped[list["Checkpoint"]] = relationship(
        back_populates="job", cascade="all, delete-orphan",
        order_by="Checkpoint.id")

    __table_args__ = (
        Index("ix_jobs_user_status", "user_id", "status"),
        Index("ix_jobs_status_lease", "status", "lease_expires_at"),
    )


class Event(Base):
    """A real metric/lifecycle event. seq is per-job, gapless for replay."""
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                default=utcnow, nullable=False)

    job: Mapped[Job] = relationship(back_populates="events")

    __table_args__ = (
        UniqueConstraint("job_id", "seq", name="uq_event_job_seq"),
    )


class Checkpoint(Base):
    __tablename__ = "checkpoints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), nullable=False)
    epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    valid: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                default=utcnow, nullable=False)

    job: Mapped[Job] = relationship(back_populates="checkpoints")

    __table_args__ = (
        Index("ix_checkpoint_job_valid", "job_id", "valid", "epoch"),
    )
