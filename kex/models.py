"""Database models.

Isolation & immutability design
--------------------------------
* Content is de-duplicated globally by SHA-256 in ``documents`` (a pure
  content-addressed blob table). Access is granted per workspace through
  ``workspace_documents`` — reuse of an identical file never leaks access.
* A ``rule_versions`` row is immutable once ``status='published'``: the
  compiled JSON snapshot and its checksum never change. Rolling back means
  pointing the workspace at a previously published version (or rebuilding a
  fresh generation for one); historical evidence is never rewritten.
* Every extraction job item binds BOTH the document SHA-256 digest and the
  rule version, so a late result can only land against the exact
  content+rules it was computed from.
* Search indices are generationed. Queries only ever read the single
  active generation; a rebuild swaps the pointer atomically.
"""
from __future__ import annotations

import datetime as _dt
from typing import Optional

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


def _utcnow() -> _dt.datetime:
    # Naive UTC everywhere: SQLite DATETIME columns store no timezone, and
    # mixing aware/naive values breaks SQLAlchemy's in-Python evaluation.
    return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------- #
# Workspaces & content
# --------------------------------------------------------------------------- #
class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    # Random bearer secret presented as `X-Workspace-Key`.
    api_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)

    state: Mapped["WorkspaceState"] = relationship(
        back_populates="workspace", uselist=False, cascade="all, delete-orphan"
    )


class Document(Base):
    """Content-addressed text blob. NOT workspace-specific on its own."""

    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    length: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)


class WorkspaceDocument(Base):
    """Per-workspace link to a content blob.

    Deleting a document removes this link; all workspace-scoped evidence
    cascades away. The global blob is deleted only when no workspace
    references it anymore (refcount handled in the service layer).
    """

    __tablename__ = "workspace_documents"
    __table_args__ = (
        UniqueConstraint("workspace_id", "document_id", name="uq_wsdoc_ws_doc"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="RESTRICT"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)


# --------------------------------------------------------------------------- #
# Rules (immutable once published)
# --------------------------------------------------------------------------- #
class RuleVersion(Base):
    __tablename__ = "rule_versions"
    __table_args__ = (
        UniqueConstraint("workspace_id", "version", name="uq_ruleversion_ws_version"),
        Index("ix_ruleversion_ws_checksum", "workspace_id", "checksum"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    # Canonical JSON serialization of the full rule set, frozen on publish.
    snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    note: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="published")
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)


class WorkspaceState(Base):
    """Single mutable pointer row per workspace: active rule + index gen."""

    __tablename__ = "workspace_states"

    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    next_rule_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    active_rule_version_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("rule_versions.id", ondelete="RESTRICT"), nullable=True
    )
    # NULL only for the brief moment at workspace bootstrap.
    active_index_generation_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("index_generations.id", ondelete="SET NULL", use_alter=True),
        nullable=True,
    )
    next_index_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    workspace: Mapped[Workspace] = relationship(back_populates="state")


# --------------------------------------------------------------------------- #
# Extraction jobs, checkpoints & entities
# --------------------------------------------------------------------------- #
class ExtractionJob(Base):
    __tablename__ = "extraction_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # incremental|rebuild
    target_rule_version_id: Mapped[int] = mapped_column(
        ForeignKey("rule_versions.id", ondelete="CASCADE"), nullable=False
    )
    # Only set for rebuild jobs: the index generation being constructed.
    target_index_generation_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("index_generations.id", ondelete="CASCADE"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[_dt.datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )
    finished_at: Mapped[Optional[_dt.datetime]] = mapped_column(DateTime, nullable=True)


class JobItem(Base):
    """Unit of work AND durable checkpoint.

    Each row binds a workspace document link, the exact content digest and
    the rule version at enqueue time. The (workspace, document, rule_version)
    unique constraint makes re-running any stage idempotent.
    """

    __tablename__ = "job_items"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "workspace_document_id",
            "rule_version_id",
            "index_generation_id",
            name="uq_jobitem_scope",
        ),
        Index("ix_jobitem_claim", "status", "workspace_id", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("extraction_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    workspace_document_id: Mapped[int] = mapped_column(
        ForeignKey("workspace_documents.id", ondelete="CASCADE"), nullable=False
    )
    # Digest snapshot taken at enqueue time; workers re-verify before writing.
    document_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    rule_version_id: Mapped[int] = mapped_column(
        ForeignKey("rule_versions.id", ondelete="CASCADE"), nullable=False
    )
    # Generation whose postings this item also populates (active or rebuilding).
    index_generation_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("index_generations.id", ondelete="CASCADE"), nullable=True
    )
    stage: Mapped[str] = mapped_column(
        String(24), nullable=False, default="extract"
    )  # queued|extract|index|done
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    leased_by: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    leased_until: Mapped[Optional[_dt.datetime]] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    updated_at: Mapped[_dt.datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow, nullable=False
    )


class Entity(Base):
    """Immutable extraction evidence.

    Both the original surface form (``text`` + char offsets) and the
    normalized canonical name are kept. Alias normalization therefore never
    loses the original mention.
    """

    __tablename__ = "entities"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "document_id",
            "rule_version_id",
            "start_char",
            "end_char",
            "entity_type",
            name="uq_entity_mention",
        ),
        Index("ix_entity_doc_rule", "workspace_document_id", "rule_version_id"),
        Index("ix_entity_canonical", "workspace_id", "entity_type", "canonical_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    workspace_document_id: Mapped[int] = mapped_column(
        ForeignKey("workspace_documents.id", ondelete="CASCADE"), nullable=False
    )
    # Denormalized global doc id, also cascade-removed via the ws link.
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    document_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    rule_version_id: Mapped[int] = mapped_column(
        ForeignKey("rule_versions.id", ondelete="CASCADE"), nullable=False
    )
    entity_type: Mapped[str] = mapped_column(String(16), nullable=False)
    text: Mapped[str] = mapped_column(String(512), nullable=False)
    canonical_name: Mapped[str] = mapped_column(String(512), nullable=False)
    start_char: Mapped[int] = mapped_column(Integer, nullable=False)
    end_char: Mapped[int] = mapped_column(Integer, nullable=False)
    matched_rule: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)


# --------------------------------------------------------------------------- #
# Generationed TF-IDF search index
# --------------------------------------------------------------------------- #
class IndexGeneration(Base):
    __tablename__ = "index_generations"
    __table_args__ = (
        UniqueConstraint("workspace_id", "generation", name="uq_indexgen_ws_gen"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    # Rule version this generation was built against (TF-IDF vocabulary is
    # bound to the corpus state of that build).
    rule_version_id: Mapped[int] = mapped_column(
        ForeignKey("rule_versions.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="building"
    )  # building|active|retired
    created_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    activated_at: Mapped[Optional[_dt.datetime]] = mapped_column(DateTime, nullable=True)


class IndexDocumentStat(Base):
    """Per-document length normalization factors for one generation."""

    __tablename__ = "index_document_stats"
    __table_args__ = (
        UniqueConstraint(
            "index_generation_id", "workspace_document_id", name="uq_idxstat_gen_doc"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    index_generation_id: Mapped[int] = mapped_column(
        ForeignKey("index_generations.id", ondelete="CASCADE"), nullable=False
    )
    workspace_document_id: Mapped[int] = mapped_column(
        ForeignKey("workspace_documents.id", ondelete="CASCADE"), nullable=False
    )
    # Sum over tokens of tf_idf^2 — used for cosine normalization.
    norm_sq: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)


class IndexTerm(Base):
    __tablename__ = "index_terms"
    __table_args__ = (
        UniqueConstraint("index_generation_id", "term", name="uq_idxterm_gen_term"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    index_generation_id: Mapped[int] = mapped_column(
        ForeignKey("index_generations.id", ondelete="CASCADE"), nullable=False
    )
    term: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    document_frequency: Mapped[int] = mapped_column(Integer, nullable=False)
    idf: Mapped[float] = mapped_column(Float, nullable=False)


class IndexPosting(Base):
    """Inverted index rows: tf/tf-idf weight per (generation, term, doc)."""

    __tablename__ = "index_postings"
    __table_args__ = (
        UniqueConstraint(
            "index_generation_id",
            "index_term_id",
            "workspace_document_id",
            name="uq_idxposting_gen_term_doc",
        ),
        Index("ix_idxposting_gen_term", "index_generation_id", "index_term_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    index_generation_id: Mapped[int] = mapped_column(
        ForeignKey("index_generations.id", ondelete="CASCADE"), nullable=False
    )
    index_term_id: Mapped[int] = mapped_column(
        ForeignKey("index_terms.id", ondelete="CASCADE"), nullable=False
    )
    workspace_document_id: Mapped[int] = mapped_column(
        ForeignKey("workspace_documents.id", ondelete="CASCADE"), nullable=False
    )
    term_frequency: Mapped[int] = mapped_column(Integer, nullable=False)
    tf_idf: Mapped[float] = mapped_column(Float, nullable=False)


class WorkerHeartbeat(Base):
    """Cluster-wide worker registry; size of this table enforces the 2-cap."""

    __tablename__ = "worker_heartbeats"

    worker_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    started_at: Mapped[_dt.datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    last_beat: Mapped[_dt.datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
