from datetime import datetime

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
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON

from workflow_engine.constants import InstanceStatus, TaskStatus, TemplateStatus

# PostgreSQL 上使用 JSONB，其他后端（SQLite 测试）回退到通用 JSON
JSONType = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class Template(Base, TimestampMixin):
    """流程模板。current_version_id 指向当前发布版本；回滚只改这个指针。"""

    __tablename__ = "templates"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    current_version_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("template_versions.id", ondelete="RESTRICT"), nullable=True
    )
    current_version_number: Mapped[int | None] = mapped_column(Integer, nullable=True)


class TemplateVersion(Base, TimestampMixin):
    """不可变的模板版本。published 之后 definition 冻结。"""

    __tablename__ = "template_versions"
    __table_args__ = (UniqueConstraint("template_id", "version", name="uq_template_version"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    template_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("templates.id", ondelete="RESTRICT"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=TemplateStatus.DRAFT)
    definition: Mapped[dict] = mapped_column(JSONType, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Instance(Base, TimestampMixin):
    """流程实例，创建时绑定具体模板版本，之后永不改变。"""

    __tablename__ = "instances"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    template_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("templates.id", ondelete="RESTRICT"), nullable=False
    )
    version_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("template_versions.id", ondelete="RESTRICT"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    business_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    submitter: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=InstanceStatus.RUNNING)
    current_node_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    context: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class NodeActivity(Base, TimestampMixin):
    """实例在某个审批节点上的一次"驻留"：承载该节点的待办与超时状态。

    条件节点是瞬时的，不产生 activity。每次进入审批节点生成一行，
    保证超时升级只发生一次、且与待办同生共死。
    """

    __tablename__ = "node_activities"
    __table_args__ = (
        Index("ix_node_activity_pending", "status", "deadline"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    instance_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("instances.id", ondelete="CASCADE"), nullable=False
    )
    node_id: Mapped[str] = mapped_column(String(100), nullable=False)
    # pending / completed / rejected / withdrawn / timeout_resolved
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    escalated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class Task(Base, TimestampMixin):
    """审批待办，挂在 NodeActivity 上。"""

    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("activity_id", "assignee", name="uq_activity_assignee"),
        Index("ix_tasks_assignee_status", "assignee", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    instance_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("instances.id", ondelete="CASCADE"), nullable=False
    )
    activity_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("node_activities.id", ondelete="CASCADE"), nullable=False
    )
    node_id: Mapped[str] = mapped_column(String(100), nullable=False)
    assignee: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=TaskStatus.PENDING)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)


class AuditLog(Base, TimestampMixin):
    """审计历史：状态机的每一次有效转换都在此留下一条记录，同事务提交。"""

    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_instance_time", "instance_id", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    instance_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("instances.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    node_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    actor: Mapped[str | None] = mapped_column(String(100), nullable=True)
    actor_type: Mapped[str] = mapped_column(String(20), nullable=False)
    detail: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    request_id: Mapped[str | None] = mapped_column(String(100), nullable=True)


class RequestLedger(Base):
    """幂等台账：只有成功提交的请求才会落库。

    冲突（409/4xx 业务错误）不写入，因此同一个 request_id 可在修正后重试。
    """

    __tablename__ = "request_ledger"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    response_payload: Mapped[dict] = mapped_column(JSONType, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
