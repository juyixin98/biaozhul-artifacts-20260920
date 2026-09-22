"""ORM 模型。所有业务表带 workspace_id（blob/规则包等全局共享资源除外）。"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    api_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    active_rule_pack_version: Mapped[str] = mapped_column(String(200), nullable=False)
    # 指向 index_generations.id；与该表存在循环引用，故用普通整数列，由服务层校验归属。
    active_index_generation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class Blob(Base):
    """全局内容寻址存储：字节按 SHA-256 去重，跨工作区共享但只经文档行授权访问。"""

    __tablename__ = "blobs"

    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    byte_length: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ref_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        # 每个工作区对同一内容哈希至多有一条「存活」文档；删除后可重新上传。
        Index(
            "uq_documents_workspace_sha_alive",
            "workspace_id",
            "doc_sha256",
            unique=True,
            sqlite_where="deleted_at IS NULL",
        ),
        Index("ix_documents_workspace_id", "workspace_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(500), nullable=False)
    doc_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    blob_sha256: Mapped[str | None] = mapped_column(
        ForeignKey("blobs.sha256", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    workspace: Mapped[Workspace] = relationship()


class RulePack(Base):
    """只追加的规则包。version 由内容哈希派生，内容永不可改。"""

    __tablename__ = "rule_packs"

    version: Mapped[str] = mapped_column(String(200), primary_key=True)
    content_json: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class RuleActivation(Base):
    """规则激活/回滚历史。只追加——回滚也写新行，永不改写。"""

    __tablename__ = "rule_activations"
    __table_args__ = (
        Index("ix_rule_activations_workspace", "workspace_id", "activated_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    rule_pack_version: Mapped[str] = mapped_column(
        ForeignKey("rule_packs.version"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(20), nullable=False)  # activate / rollback
    previous_version: Mapped[str | None] = mapped_column(String(200), nullable=True)
    activated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_claim", "status", "kind", "priority", "id"),
        Index("ix_jobs_workspace", "workspace_id"),
        Index("ix_jobs_document", "document_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[int | None] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # extract / rebuild
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    # pending / running / succeeded / failed
    rule_pack_version: Mapped[str] = mapped_column(String(200), nullable=False)
    doc_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_stage: Mapped[str | None] = mapped_column(String(30), nullable=True)
    error_doc_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    checkpoint_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    leased_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    leased_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Checkpoint(Base):
    """命名检查点（重建代次进度等），持久化以便重启恢复。"""

    __tablename__ = "checkpoints"
    __table_args__ = (UniqueConstraint("workspace_id", "name", name="uq_checkpoints_ws_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    value_json: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class WorkerRegistry(Base):
    __tablename__ = "worker_registry"

    worker_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class Entity(Base):
    __tablename__ = "entities"
    __table_args__ = (
        # 同一文档在同一规则版本下，同一规范实体/类型只写一行——重复执行幂等。
        UniqueConstraint(
            "document_id",
            "rule_pack_version",
            "entity_type",
            "canonical",
            name="uq_entities_doc_ver_type_canonical",
        ),
        Index("ix_entities_doc", "document_id"),
        Index("ix_entities_ws_type_canon", "workspace_id", "entity_type", "canonical"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    rule_pack_version: Mapped[str] = mapped_column(String(200), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(20), nullable=False)
    canonical: Mapped[str] = mapped_column(String(500), nullable=False)
    first_seen_rule_id: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class Mention(Base):
    """原始提及：别名归一化绝不丢原文位置。"""

    __tablename__ = "mentions"
    __table_args__ = (
        UniqueConstraint(
            "entity_id", "char_start", "char_end", "matched_text", name="uq_mentions_span"
        ),
        Index("ix_mentions_entity", "entity_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entity_id: Mapped[int] = mapped_column(
        ForeignKey("entities.id", ondelete="CASCADE"), nullable=False
    )
    alias: Mapped[str] = mapped_column(String(500), nullable=False)
    matched_text: Mapped[str] = mapped_column(String(500), nullable=False)
    char_start: Mapped[int] = mapped_column(Integer, nullable=False)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False)
    rule_id: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class IndexGeneration(Base):
    """索引代次。每工作区至多一个 active（部分唯一索引），切换在单事务内完成。"""

    __tablename__ = "index_generations"
    __table_args__ = (
        Index(
            "uq_index_generations_one_active",
            "workspace_id",
            unique=True,
            sqlite_where="status = 'active'",
        ),
        Index("ix_index_gen_ws_status", "workspace_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    rule_pack_version: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="building")
    # building / active / superseded / failed / abandoned
    doc_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class IndexDf(Base):
    """代次级文档频率（决定 IDF）。finalize 时从 posting 重算。"""

    __tablename__ = "index_df"
    __table_args__ = (
        UniqueConstraint("generation_id", "term", name="uq_index_df_gen_term"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    generation_id: Mapped[int] = mapped_column(
        ForeignKey("index_generations.id", ondelete="CASCADE"), nullable=False
    )
    term: Mapped[str] = mapped_column(String(200), nullable=False)
    df: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class DocTermStat(Base):
    """代次×文档×词：词频与归一化权重。"""

    __tablename__ = "doc_term_stats"
    __table_args__ = (
        UniqueConstraint(
            "generation_id", "document_id", "term", name="uq_doc_term_stats_gen_doc_term"
        ),
        Index("ix_doc_term_stats_gen_doc", "generation_id", "document_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    generation_id: Mapped[int] = mapped_column(
        ForeignKey("index_generations.id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    term: Mapped[str] = mapped_column(String(200), nullable=False)
    tf: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    weight: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)


class Posting(Base):
    """倒排表：词 -> 命中文档及 Unicode 码点位置（稳定排序/高亮用）。"""

    __tablename__ = "postings"
    __table_args__ = (
        UniqueConstraint(
            "generation_id",
            "document_id",
            "term",
            "char_start",
            name="uq_postings_gen_doc_term_pos",
        ),
        Index("ix_postings_gen_term", "generation_id", "term"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    generation_id: Mapped[int] = mapped_column(
        ForeignKey("index_generations.id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    term: Mapped[str] = mapped_column(String(200), nullable=False)
    char_start: Mapped[int] = mapped_column(Integer, nullable=False)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False)
