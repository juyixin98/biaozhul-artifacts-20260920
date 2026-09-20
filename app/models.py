"""Database models.

Architectures and datasets are immutable once created (the schema never
updates their spec/path after insert). A job binds a specific architecture
id, dataset id, the split indices, seed and hyperparameters, so a training
run is fully reproducible from its row.
"""
from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JobStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class Architecture(Base):
    __tablename__ = "architectures"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # JSON definition: {"input_features": int, "layers": [...]}.
    spec: Mapped[dict] = mapped_column(JSON, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    param_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    jobs: Mapped[list["Job"]] = relationship(back_populates="architecture")


class Dataset(Base):
    __tablename__ = "datasets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    fmt: Mapped[str] = mapped_column(String(10), nullable=False)  # csv | npy
    # "classification" or "regression"; selects the loss function.
    task: Mapped[str] = mapped_column(String(20), nullable=False)
    num_rows: Mapped[int] = mapped_column(Integer, nullable=False)
    num_features: Mapped[int] = mapped_column(Integer, nullable=False)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    jobs: Mapped[list["Job"]] = relationship(back_populates="dataset")


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)

    architecture_id: Mapped[int] = mapped_column(
        ForeignKey("architectures.id"), nullable=False
    )
    dataset_id: Mapped[int] = mapped_column(ForeignKey("datasets.id"), nullable=False)

    # Full reproducibility snapshot.
    hyperparams: Mapped[dict] = mapped_column(JSON, nullable=False)
    seed: Mapped[int] = mapped_column(Integer, nullable=False)
    dataset_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    # {"train": [indices], "val": [indices]} — fixed at creation.
    split: Mapped[dict] = mapped_column(JSON, nullable=False)
    epochs_completed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_epochs: Mapped[int] = mapped_column(Integer, nullable=False)

    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, native_enum=False, length=20),
        nullable=False,
        default=JobStatus.QUEUED,
        index=True,
    )
    # Executor identity + lease fencing.
    executor_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    architecture: Mapped[Architecture] = relationship(back_populates="jobs")
    dataset: Mapped[Dataset] = relationship(back_populates="jobs")
    events: Mapped[list["Event"]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="Event.seq"
    )
    checkpoints: Mapped[list["CheckpointRef"]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="CheckpointRef.epoch"
    )


class Event(Base):
    """Per-job append-only event log; seq is gapless and SSE-resumable."""

    __tablename__ = "events"
    __table_args__ = (UniqueConstraint("job_id", "seq", name="uq_event_job_seq"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    # "status" | "metrics" | "log"
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    job: Mapped[Job] = relationship(back_populates="events")


class CheckpointRef(Base):
    """Published checkpoint references; one row per completed epoch.

    The file is fsynced and atomically renamed into place *before* the row is
    inserted, so a published ref always points at a complete checkpoint.
    """

    __tablename__ = "checkpoint_refs"
    __table_args__ = (
        UniqueConstraint("job_id", "epoch", name="uq_checkpoint_job_epoch"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    valid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    job: Mapped[Job] = relationship(back_populates="checkpoints")
