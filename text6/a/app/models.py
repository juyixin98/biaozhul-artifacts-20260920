"""SQLAlchemy models for ConsentVault.

Design notes
------------
* ``consent_events`` is append-only and *immutable*: rows are never updated or
  deleted after commit (enforced by a trigger).  Current consent state is a
  projection that can always be rebuilt from this history.
* ``event_idempotency`` records the client-supplied event id and the full
  request fingerprint, so a repeated request returns the original result while
  the same id carrying different content is rejected as a conflict.
* ``consent_states`` carries a version counter used for optimistic concurrency
  control: writes must present the version they expect.
* On subject erasure the subject row and every personal field are destroyed;
  ``consent_events`` keep only operational pseudonyms (hashes) and ``audit_logs``
  never contain personal data in the first place.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    api_keys: Mapped[list["ApiKey"]] = relationship(back_populates="organization")


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    # Store a SHA-256 hex digest only, never the raw key.
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    active: Mapped[bool] = mapped_column(nullable=False, default=True, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    organization: Mapped[Organization] = relationship(back_populates="api_keys")

    __table_args__ = (CheckConstraint("role in ('admin','auditor')", name="api_key_role_chk"),)


class Subject(Base):
    __tablename__ = "subjects"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    # Pseudonymous local identifier; this plus the export copy is what gets
    # destroyed on erasure.
    subject_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    # Stable SHA-256 over (org, subject_ref) kept in immutable history after the
    # identifiable mapping has been deleted.  Not reversible.
    subject_pseudonym: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    __table_args__ = (
        UniqueConstraint("organization_id", "subject_ref", name="uq_subject_org_ref"),
        UniqueConstraint(
            "organization_id", "subject_pseudonym", name="uq_subject_org_pseudonym"
        ),
    )


class SubjectExportCopy(Base):
    """Records that an identifiable copy of the subject exists downstream.

    Erasure must clear all of these together with the identifiable mapping.
    Only a label is stored, never personal data.
    """

    __tablename__ = "subject_export_copies"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    organization_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    subject_id: Mapped[int] = mapped_column(
        ForeignKey("subjects.id", ondelete="CASCADE"), nullable=False
    )
    copy_label: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    __table_args__ = (
        Index("ix_export_copies_subject", "subject_id"),
    )


class Purpose(Base):
    __tablename__ = "purposes"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    __table_args__ = (
        UniqueConstraint("organization_id", "key", name="uq_purpose_org_key"),
    )


class ConsentPolicy(Base):
    __tablename__ = "consent_policies"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    purpose_id: Mapped[int] = mapped_column(
        ForeignKey("purposes.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    __table_args__ = (
        UniqueConstraint("organization_id", "purpose_id", name="uq_policy_org_purpose"),
    )


class PolicyVersion(Base):
    __tablename__ = "policy_versions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    policy_id: Mapped[int] = mapped_column(
        ForeignKey("consent_policies.id", ondelete="RESTRICT"), nullable=False
    )
    organization_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    version: Mapped[int] = mapped_column(nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    __table_args__ = (
        UniqueConstraint("policy_id", "version", name="uq_policy_version"),
        Index("ix_policy_versions_org", "organization_id"),
    )


class ConsentState(Base):
    """Mutable projection of the event stream; rebuildable at any time."""

    __tablename__ = "consent_states"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    organization_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    subject_id: Mapped[int] = mapped_column(
        ForeignKey("subjects.id", ondelete="CASCADE"), nullable=False
    )
    purpose_id: Mapped[int] = mapped_column(
        ForeignKey("purposes.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    version: Mapped[int] = mapped_column(nullable=False, default=0, server_default=text("0"))
    policy_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("policy_versions.id", ondelete="RESTRICT"), nullable=True
    )
    granted_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("consent_events.id", ondelete="RESTRICT"), nullable=True
    )
    granted_event_sequence: Mapped[int | None] = mapped_column(nullable=True)
    granted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    withdrawn_event_id: Mapped[int | None] = mapped_column(nullable=True)
    withdrawn_event_sequence: Mapped[int | None] = mapped_column(nullable=True)
    withdrawn_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    __table_args__ = (
        UniqueConstraint("subject_id", "purpose_id", name="uq_state_subject_purpose"),
        Index("ix_consent_states_org", "organization_id"),
        CheckConstraint(
            "status in ('granted','withdrawn')", name="consent_state_status_chk"
        ),
    )


class ConsentEvent(Base):
    """Append-only, immutable history row."""

    __tablename__ = "consent_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    organization_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Subject pseudonym (hash) rather than the identifiable reference, so the
    # history remains usable after erasure without containing personal data.
    subject_pseudonym: Mapped[str] = mapped_column(String(64), nullable=False)
    purpose_key: Mapped[str] = mapped_column(String(100), nullable=False)
    event_type: Mapped[str] = mapped_column(String(20), nullable=False)
    sequence: Mapped[int] = mapped_column(nullable=False)
    policy_version: Mapped[int | None] = mapped_column(nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    __table_args__ = (
        UniqueConstraint(
            "organization_id", "subject_pseudonym", "purpose_key", "sequence",
            name="uq_event_sequence",
        ),
        Index("ix_consent_events_lookup",
              "organization_id", "subject_pseudonym", "purpose_key"),
        CheckConstraint(
            "event_type in ('grant','withdrawal')", name="consent_event_type_chk"
        ),
    )


class EventIdempotency(Base):
    __tablename__ = "event_idempotency"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    organization_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    subject_id: Mapped[int | None] = mapped_column(
        ForeignKey("subjects.id", ondelete="SET NULL"), nullable=True
    )
    event_id: Mapped[str] = mapped_column(String(100), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    # Snapshot of the response returned by the first successful request.
    # Replaced by '{}' on erasure while the fingerprint (a hash) is retained as
    # a tombstone so delayed retries cannot recreate a deleted subject.
    response_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    erased: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default=text("false")
    )
    consent_event_id: Mapped[int | None] = mapped_column(
        ForeignKey("consent_events.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    __table_args__ = (
        UniqueConstraint("organization_id", "event_id", name="uq_idempotency_event"),
    )


class AuditLog(Base):
    """Operational audit trail. Never stores personal fields."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    organization_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Pseudonym of the acting operator key (hash), not the raw key.
    actor_pseudonym: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_role: Mapped[str] = mapped_column(String(20), nullable=False)
    action: Mapped[str] = mapped_column(String(60), nullable=False)
    # Stable entity references (ids / purpose keys) — no subject personal data.
    detail_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    __table_args__ = (Index("ix_audit_logs_org_time", "organization_id", "occurred_at"),)
