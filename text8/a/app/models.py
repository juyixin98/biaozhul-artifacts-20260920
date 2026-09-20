"""SQLAlchemy ORM 模型。

关键不变量：
- instance.template_version_id 绑定后永不改变（版本隔离）。
- task 行内置状态机：pending -> approved/rejected/canceled，只允许一次有效转换，
  依赖行级锁 + 条件 UPDATE 保证并发下只生效一次。
- escalation 是持久化的升级任务，worker 崩溃重启后仍可继续处理。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_uuid() -> str:
    return str(uuid.uuid4())


class Template(Base):
    __tablename__ = "template"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    current_version_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "template_version.id",
            name="fk_template_current_version",
            use_alter=True,
        ),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    versions: Mapped[list["TemplateVersion"]] = relationship(
        back_populates="template",
        cascade="all, delete-orphan",
        order_by="TemplateVersion.version",
        foreign_keys="TemplateVersion.template_id",
    )
    current_version: Mapped["TemplateVersion | None"] = relationship(
        foreign_keys=[current_version_id], post_update=True
    )


class TemplateVersion(Base):
    __tablename__ = "template_version"
    __table_args__ = (
        UniqueConstraint("template_id", "version", name="uq_template_version"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    template_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("template.id"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    definition: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    template: Mapped[Template] = relationship(
        back_populates="versions", foreign_keys=[template_id]
    )


class Instance(Base):
    __tablename__ = "instance"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    template_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("template.id"), nullable=False
    )
    # 绑定的发布版本，永不更新 —— 版本隔离的核心。
    template_version_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("template_version.id"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    submitter: Mapped[str] = mapped_column(String(64), nullable=False)
    context: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    current_node_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_request_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    tasks: Mapped[list["Task"]] = relationship(
        back_populates="instance", cascade="all, delete-orphan"
    )
    history: Mapped[list["HistoryEvent"]] = relationship(
        back_populates="instance", cascade="all, delete-orphan", order_by="HistoryEvent.id"
    )


class Task(Base):
    __tablename__ = "task"
    __table_args__ = (
        Index("ix_task_instance_node", "instance_id", "node_id"),
        # 同一节点激活中，一个审批人只有一条待办（升级改派后旧待办 canceled，
        # 新待办是新行，因此这里不加唯一约束，状态在应用层判定）。
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    instance_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("instance.id"), nullable=False
    )
    node_id: Mapped[str] = mapped_column(String(64), nullable=False)
    assignee: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    decision: Mapped[str | None] = mapped_column(String(16), nullable=True)
    handled_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    handled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    instance: Mapped[Instance] = relationship(back_populates="tasks")


class HistoryEvent(Base):
    """审计历史，只追加，不更新不删除。"""

    __tablename__ = "history_event"
    __table_args__ = (Index("ix_history_instance", "instance_id", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    instance_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("instance.id"), nullable=False
    )
    node_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    instance: Mapped[Instance] = relationship(back_populates="history")


class Escalation(Base):
    """节点超时升级任务。每个节点激活至多一行，status 保证只处理一次。"""

    __tablename__ = "escalation"
    __table_args__ = (
        UniqueConstraint("instance_id", "node_id", name="uq_escalation_instance_node"),
        Index("ix_escalation_due", "status", "due_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    instance_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("instance.id"), nullable=False
    )
    node_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    targets: Mapped[list] = mapped_column(JSONB, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class IdempotencyRecord(Base):
    """请求幂等记录。request_id 全局唯一；指纹冲突返回 409，且不写业务数据。"""

    __tablename__ = "idempotency_record"

    request_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    fingerprint: Mapped[str] = mapped_column(Text, nullable=False)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
