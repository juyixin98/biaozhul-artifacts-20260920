"""Subject lifecycle: creation, lookup, export-copy bookkeeping and erasure."""
from __future__ import annotations

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    AuditAction,
    AuditLog,
    ConsentEvent,
    ExportCopy,
    Subject,
)
from app.services.errors import ConflictError, GoneError, NotFoundError
from app.services.state import utcnow


def create_subject(
    db: Session,
    *,
    organization_id: int,
    api_key_id: int,
    external_ref: str,
    email: str | None,
    display_name: str | None,
) -> Subject:
    subject = Subject(
        organization_id=organization_id,
        external_ref=external_ref,
        email=email,
        display_name=display_name,
    )
    db.add(subject)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise ConflictError("external_ref already exists in your organization")

    db.add(
        AuditLog(
            organization_id=organization_id,
            api_key_id=api_key_id,
            action=AuditAction.subject_created,
            # No personal fields -- only the opaque internal id.
            detail={"subject_id": subject.id},
        )
    )
    db.commit()
    db.refresh(subject)
    return subject


def get_subject(db: Session, *, organization_id: int, subject_id: int) -> Subject:
    subject = (
        db.query(Subject)
        .filter(Subject.id == subject_id, Subject.organization_id == organization_id)
        .one_or_none()
    )
    if subject is None:
        raise NotFoundError("subject not found in your organization")
    return subject


def resolve_by_ref(db: Session, *, organization_id: int, external_ref: str) -> Subject:
    """Resolve an external identifier. Returns 404 after erasure because the
    identifying mapping has been destroyed."""
    subject = (
        db.query(Subject)
        .filter(
            Subject.organization_id == organization_id,
            Subject.external_ref == external_ref,
        )
        .one_or_none()
    )
    if subject is None:
        raise NotFoundError("external_ref not found in your organization")
    return subject


def register_export(
    db: Session,
    *,
    organization_id: int,
    api_key_id: int,
    subject_id: int,
    destination: str,
    payload: str,
) -> ExportCopy:
    subject = get_subject(db, organization_id=organization_id, subject_id=subject_id)
    if subject.erased:
        raise GoneError("subject has been erased")
    export = ExportCopy(
        organization_id=organization_id,
        subject_id=subject_id,
        destination=destination,
        payload=payload,
    )
    db.add(export)
    db.flush()
    db.add(
        AuditLog(
            organization_id=organization_id,
            api_key_id=api_key_id,
            action=AuditAction.export_registered,
            detail={"subject_id": subject_id, "export_id": export.id},
        )
    )
    db.commit()
    db.refresh(export)
    return export


def erase_subject(
    db: Session, *, organization_id: int, api_key_id: int, subject_id: int
) -> Subject:
    """Right-to-erasure implementation.

    * identifying attributes and the external mapping are cleared;
    * every export copy (the PII payloads) is deleted;
    * the immutable consent ledger survives but references only an opaque,
      anonymous numeric subject id;
    * the audit trail keeps a PII-free operational record.
    """
    subject = get_subject(db, organization_id=organization_id, subject_id=subject_id)
    if subject.erased:
        raise GoneError("subject is already erased")

    subject.external_ref = None
    subject.email = None
    subject.display_name = None
    subject.erased = True
    subject.erased_at = utcnow()

    deleted_exports = (
        db.query(ExportCopy)
        .filter(
            ExportCopy.organization_id == organization_id,
            ExportCopy.subject_id == subject_id,
        )
        .delete(synchronize_session=False)
    )

    # The materialised consent state and the append-only event ledger carry
    # no personal data and are keyed by an opaque numeric subject id, so they
    # are retained for audit; verification on the internal id keeps working.
    remaining_events = (
        db.query(ConsentEvent)
        .filter(
            ConsentEvent.organization_id == organization_id,
            ConsentEvent.subject_id == subject_id,
        )
        .count()
    )

    db.add(
        AuditLog(
            organization_id=organization_id,
            api_key_id=api_key_id,
            action=AuditAction.subject_erased,
            detail={
                "subject_id": subject_id,
                "deleted_export_copies": deleted_exports,
                "ledger_events_retained": remaining_events,
            },
        )
    )
    db.commit()
    db.refresh(subject)
    return subject
