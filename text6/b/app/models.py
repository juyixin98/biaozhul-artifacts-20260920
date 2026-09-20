"""SQLAlchemy ORM models for ConsentVault.

Design notes
------------
* Consent is **event-sourced per stream** -- a stream is identified by
  ``(organization_id, subject_id, purpose)``. ``consent_events`` is an
  append-only ledger (guarded at the database level by triggers);
  ``consent_states`` is a materialised view rebuilt from the ledger.
* ``policy_versions`` is also append-only: a published version can never be
  modified, only superseded by publishing a newer version.
* Subjects carry identifying attributes; on erasure those columns are cleared
  and the row is tombstoned, while the PII-free ledger remains for audit.
"""
from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Role(str, enum.Enum):
    admin = "admin"
    auditor = "auditor"  # read-only


class EventType(str, enum.Enum):
    grant = "grant"
    withdraw = "withdraw"


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    api_keys: Mapped[list["ApiKey"]] = relationship(back_populates="organization")


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    # Only a SHA-256 hash of the key is stored -- never the secret itself.
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    role: Mapped[Role] = mapped_column(Enum(Role, name="api_key_role"), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    organization: Mapped[Organization] = relationship(back_populates="api_keys")


class Subject(Base):
    __tablename__ = "subjects"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    # Caller-supplied identifier, cleared on erasure so it can no longer be
    # resolved back to this (now opaque) subject id.
    external_ref: Mapped[str | None] = mapped_column(String(300), nullable=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    erased: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    erased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("organization_id", "external_ref", name="uq_subjects_org_external_ref"),
        Index("ix_subjects_org", "organization_id"),
    )


class ExportCopy(Base):
    """Tracks a data export handed to a subject / third party.

    The copy payload itself (the PII) lives in ``payload`` and is deleted when
    the subject is erased; only this bookkeeping row is required, and erasure
    removes the whole row.
    """

    __tablename__ = "export_copies"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    subject_id: Mapped[int] = mapped_column(
        ForeignKey("subjects.id", ondelete="RESTRICT"), nullable=False
    )
    destination: Mapped[str] = mapped_column(String(300), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PolicyVersion(Base):
    """An immutable, published policy document.

    ``version`` is a caller-chosen label (e.g. ``v2026-01``); once published
    the row never changes -- enforced by a database trigger.
    """

    __tablename__ = "policy_versions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    version: Mapped[str] = mapped_column(String(100), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("organization_id", "version", name="uq_policy_org_version"),
    )


class ConsentEvent(Base):
    """Append-only consent ledger entry.

    ``version`` is the *stream version* created by this event (1, 2, 3 ...).
    ``event_id`` is the client supplied idempotency key and ``fingerprint`` is
    a canonical hash of the request semantics -- a reused ``event_id`` with a
    different fingerprint is rejected as a conflict.
    """

    __tablename__ = "consent_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_id: Mapped[str] = mapped_column(String(100), nullable=False)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    subject_id: Mapped[int] = mapped_column(
        ForeignKey("subjects.id", ondelete="RESTRICT"), nullable=False
    )
    purpose: Mapped[str] = mapped_column(String(200), nullable=False)
    event_type: Mapped[EventType] = mapped_column(
        Enum(EventType, name="consent_event_type"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_version_id: Mapped[int | None] = mapped_column(
        ForeignKey("policy_versions.id", ondelete="RESTRICT"), nullable=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    policy_version: Mapped[PolicyVersion | None] = relationship()

    __table_args__ = (
        UniqueConstraint("organization_id", "event_id", name="uq_consent_event_org_event_id"),
        UniqueConstraint(
            "organization_id",
            "subject_id",
            "purpose",
            "version",
            name="uq_consent_stream_version",
        ),
        Index("ix_consent_events_stream", "organization_id", "subject_id", "purpose"),
    )


class ConsentState(Base):
    """Materialised current state of one consent stream, rebuilt from events."""

    __tablename__ = "consent_states"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    subject_id: Mapped[int] = mapped_column(
        ForeignKey("subjects.id", ondelete="RESTRICT"), nullable=False
    )
    purpose: Mapped[str] = mapped_column(String(200), nullable=False)
    last_event_id: Mapped[int] = mapped_column(
        ForeignKey("consent_events.id", ondelete="RESTRICT"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "subject_id",
            "purpose",
            name="uq_consent_state_stream",
        ),
    )


class AuditAction(str, enum.Enum):
    subject_created = "subject_created"
    subject_erased = "subject_erased"
    policy_published = "policy_published"
    consent_granted = "consent_granted"
    consent_withdrawn = "consent_withdrawn"
    consent_batch_imported = "consent_batch_imported"
    export_registered = "export_registered"
    states_rebuilt = "states_rebuilt"


class AuditLog(Base):
    """Operational audit record.

    Deliberately contains **no personal fields**: subject ids are opaque
    numeric ids (not identities) and request bodies / emails / external
    references are never copied here.
    """

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    api_key_id: Mapped[int | None] = mapped_column(
        ForeignKey("api_keys.id", ondelete="RESTRICT"), nullable=True
    )
    action: Mapped[AuditAction] = mapped_column(Enum(AuditAction, name="audit_action"), nullable=False)
    # Small PII-free context bag, e.g. {"purpose": "marketing", "events": 12}.
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_audit_logs_org_created", "organization_id", "created_at"),
    )
