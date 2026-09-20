"""SQLAlchemy ORM models.

Design overview
---------------
``consent_events`` is the single source of truth: an append-only, immutable
ledger. Rows may never be UPDATE'd or DELETE'd (a database trigger enforces
this, see the Alembic migration). Every grant, withdrawal and expiration
recorded there is kept forever as the audit trail.

``consent_states`` is a *derived* materialisation of the ledger: one row per
(organization, subject, purpose). It is maintained transactionally as events
arrive and can at any time be discarded and rebuilt from the ledger with
identical results (``app/replay.py``).

``subjects`` / ``subject_exports`` hold the only personal data. On subject
erasure the rows are deleted or anonymised; the ledger is preserved but every
column that could identify a person in it is nulled. ``subject_deletions``
records the tombstone so that a replay after erasure cannot resurrect a state.

``audit_records`` contains operational metadata only (action, outcome,
event/version identifiers) and deliberately no personal fields.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

# Status values stored on the materialised state row.
STATUS_GRANTED = "granted"
STATUS_WITHDRAWN = "withdrawn"

# Actions that may appear in the immutable ledger.
ACTION_GRANT = "grant"
ACTION_WITHDRAW = "withdraw"


def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class ApiKey(Base):
    """Bearer credential. ``admin`` may write; ``auditor`` is strictly read-only."""

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    key_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    organization: Mapped[Organization] = relationship()

    __table_args__ = (CheckConstraint("role IN ('admin','auditor')", name="api_keys_role_check"),)


class Subject(Base):
    __tablename__ = "subjects"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    # External identifier as supplied by the organization (user id, email hash,
    # ...). Nulled when the subject is erased. A partial unique index allows a
    # fresh subject with the same key to be created after erasure.
    subject_key: Mapped[str | None] = mapped_column(String(400), nullable=True)
    erased: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    erased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index(
            "ux_subjects_org_key_live",
            "organization_id",
            "subject_key",
            unique=True,
            postgresql_where=text("erased = false"),
        ),
    )


class PolicyVersion(Base):
    """A published, immutable policy document version.

    Published rows can never be modified or removed through the API; the
    migration additionally installs triggers that reject UPDATE/DELETE.
    A new grant always pins the exact version current at grant time, so
    publishing a new version never implicitly extends an existing grant.
    """

    __tablename__ = "policy_versions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint("organization_id", "version", name="ux_policy_org_version"),
    )


class ConsentEvent(Base):
    """Append-only immutable ledger entry — the authoritative record."""

    __tablename__ = "consent_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # Client-supplied idempotency key, unique within an organization.
    event_id: Mapped[str] = mapped_column(String(100), nullable=False)
    organization_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    subject_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("subjects.id", ondelete="RESTRICT"), nullable=False
    )
    # Snapshot of the subject's external key at event time. Nulled on erasure so
    # the preserved ledger carries no identifying value afterwards.
    subject_key_snapshot: Mapped[str | None] = mapped_column(String(400), nullable=True)
    purpose: Mapped[str] = mapped_column(String(200), nullable=False)
    action: Mapped[str] = mapped_column(String(20), nullable=False)

    # Policy version pinned at grant time. Null only for withdrawals, which do
    # not (re)bind a policy.
    policy_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Optimistic-concurrency token expected by the client: the state version
    # that must be current for the write to apply. 0 means "no prior state".
    expected_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Monotonic per-(org,subject,purpose) sequence assigned by the server.
    state_version: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # SHA-256 of the normalized request payload. A repeated event_id with a
    # different hash is a conflict, never a silent overwrite.
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint("organization_id", "event_id", name="ux_events_org_event_id"),
        Index("ix_events_org_subject_purpose", "organization_id", "subject_id", "purpose"),
        CheckConstraint("action IN ('grant','withdraw')", name="consent_events_action_check"),
    )


class ConsentState(Base):
    """Derived materialised state; rebuildable from ``consent_events``."""

    __tablename__ = "consent_states"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    subject_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("subjects.id", ondelete="CASCADE"), nullable=False
    )
    purpose: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False)

    granted_event_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    latest_event_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    policy_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint(
            "organization_id", "subject_id", "purpose", name="ux_states_org_subject_purpose"
        ),
        CheckConstraint("status IN ('granted','withdrawn')", name="consent_states_status_check"),
    )


class SubjectExport(Base):
    """A generated copy/export of subject data. Removed on subject erasure."""

    __tablename__ = "subject_exports"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    subject_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("subjects.id", ondelete="CASCADE"), nullable=False
    )
    label: Mapped[str] = mapped_column(String(200), nullable=False, default="dsar-export")
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class SubjectDeletion(Base):
    """Tombstone recording that a subject was erased, so a replay cannot
    resurrect a materialised state for them. Contains no personal fields."""

    __tablename__ = "subject_deletions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    subject_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    deleted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        UniqueConstraint("organization_id", "subject_id", name="ux_subject_deletions_subject"),
    )


class AuditRecord(Base):
    """Operational audit trail. Personal fields are intentionally absent."""

    __tablename__ = "audit_records"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    subject_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    event_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    policy_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )

    __table_args__ = (
        Index("ix_audit_org_created", "organization_id", "created_at"),
        CheckConstraint("outcome IN ('success','failure')", name="audit_outcome_check"),
    )
