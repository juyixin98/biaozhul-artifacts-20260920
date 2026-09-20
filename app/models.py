"""SQLAlchemy 2.0 ORM models.

A *published* template version is immutable: publishing or rolling back always
appends a new row, never overwrites one.  Instances pin (template_code,
template_version) and running instances are therefore unaffected by later
publishes.
"""
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.constants import INSTANCE_RUNNING, TASK_PENDING
from app.db import Base


class TemplateVersion(Base):
    __tablename__ = "template_versions"

    template_code: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    definition: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_template_version_positive"),
    )


class Instance(Base):
    __tablename__ = "instances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    template_code: Mapped[str] = mapped_column(String(64), nullable=False)
    template_version: Mapped[int] = mapped_column(Integer, nullable=False)
    business_key: Mapped[str] = mapped_column(String(128), nullable=False)
    variables: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=INSTANCE_RUNNING, index=True
    )
    current_node_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    submitter: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    tasks: Mapped[list["Task"]] = relationship(
        back_populates="instance", cascade="all, delete-orphan", order_by="Task.id"
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["template_code", "template_version"],
            ["template_versions.template_code", "template_versions.version"],
        ),
        Index("uq_instance_business_key", "template_code", "business_key", unique=True),
    )


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instance_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("instances.id", ondelete="CASCADE"),
        nullable=False,
    )
    node_id: Mapped[str] = mapped_column(String(64), nullable=False)
    assignee: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=TASK_PENDING, index=True
    )
    sign_strategy: Mapped[str] = mapped_column(String(8), nullable=False)
    decided_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    escalation_target: Mapped[str | None] = mapped_column(String(64), nullable=True)
    escalated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    instance: Mapped["Instance"] = relationship(back_populates="tasks")

    __table_args__ = (
        Index("ix_tasks_pending_due", "due_at", "status"),
        # one task row per approver at a node; duplicates (e.g. an escalation
        # target that is already an approver) are rejected at publish time
        Index("uq_task_node_assignee",
              "instance_id", "node_id", "assignee", unique=True),
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instance_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    template_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    template_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    node_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actor: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )


class IdempotencyRecord(Base):
    """Stored result keyed by client request id.

    A conflicting request (same request id, different action/instance) gets
    HTTP 409 and is never written here; replays return the stored response.
    """

    __tablename__ = "idempotency_records"

    request_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    instance_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    method: Mapped[str] = mapped_column(String(64), nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    response_body: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
