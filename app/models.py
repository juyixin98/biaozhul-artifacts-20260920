"""SQLAlchemy ORM models.

Design notes
------------
* ``Template`` / ``TemplateVersion`` keep published versions immutable.
  ``Template.current_version`` is only a pointer; rolling back repoints it.
* ``Instance`` carries a frozen *snapshot* of the definition it was bound to,
  so later template edits never affect running flows.
* ``Task`` is an approval todo; ``ApprovalRequest`` provides idempotency keyed
  by the client supplied request_id and stores the first response for replay.
* ``ScheduledEscalation`` is the durable work queue used by the restart-safe
  escalation scheduler (no external message queue).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def gen_uuid() -> uuid.UUID:
    return uuid.uuid4()


class Template(Base):
    __tablename__ = "templates"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    # Pointer only — the pointed-at published row is never mutated.
    current_version: Mapped[int | None] = mapped_column(Integer, nullable=True)

    versions: Mapped[list["TemplateVersion"]] = relationship(
        back_populates="template", order_by="TemplateVersion.version"
    )


class TemplateVersion(Base):
    __tablename__ = "template_versions"
    __table_args__ = (UniqueConstraint("template_id", "version", name="uq_template_version"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    template_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("templates.id"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")  # draft|published
    definition: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    template: Mapped[Template] = relationship(back_populates="versions")


class Instance(Base):
    __tablename__ = "instances"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    template_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("templates.id"), nullable=False
    )
    template_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("template_versions.id"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    # Immutable frozen definition; running flows ignore later template edits.
    definition_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    business_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    submitter: Mapped[str] = mapped_column(String(128), nullable=False)
    # Data made available to condition expressions.
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="running", index=True
    )  # running|completed|rejected|withdrawn
    current_node_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    tasks: Mapped[list["Task"]] = relationship(back_populates="instance")


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        # One open todo per (node activation, assignee); duplicates cannot be
        # inserted even under concurrent schedulers.
        Index(
            "ix_tasks_open",
            "instance_id",
            "node_id",
            "generation",
            "assignee",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    instance_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instances.id"), nullable=False
    )
    node_id: Mapped[str] = mapped_column(String(128), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    assignee: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", index=True
    )  # pending|approved|rejected|closed|escalated
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    instance: Mapped[Instance] = relationship(back_populates="tasks")


class ApprovalRequest(Base):
    """Idempotency record for an approve/reject call.

    request_id is globally unique: the first call wins and its response is
    replayed to every later duplicate. A later duplicate whose other fields
    disagree is a conflict that never reaches history.
    """

    __tablename__ = "approval_requests"

    request_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    instance_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instances.id"), nullable=False
    )
    expected_version: Mapped[int] = mapped_column(Integer, nullable=False)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)  # approve|reject
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Stored JSON response of the first call, returned to duplicates.
    response_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class HistoryRecord(Base):
    __tablename__ = "history_records"
    __table_args__ = (Index("ix_history_instance_seq", "instance_id", "seq"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    instance_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instances.id"), nullable=False
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    node_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    actor: Mapped[str | None] = mapped_column(String(128), nullable=True)
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ScheduledEscalation(Base):
    """Durable one-shot escalation job per approval-node activation."""

    __tablename__ = "scheduled_escalations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    instance_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instances.id"), nullable=False
    )
    node_id: Mapped[str] = mapped_column(String(128), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    # False while waiting, True once fired/cancelled — only one transition ever.
    done: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
