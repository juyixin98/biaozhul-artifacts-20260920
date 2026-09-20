"""Subject erasure, data-export copies and audit recording.

Erasure removes every identifiable value and every export copy while keeping
the immutable ledger's *operational* facts (who-organisation, purpose, action,
versions) so consent history stays attributable without identifying anyone.
"""
from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.errors import NotFound
from app.models import (
    AuditRecord,
    ConsentEvent,
    ConsentState,
    Subject,
    SubjectDeletion,
    SubjectExport,
    utcnow,
)


def record_audit(
    db: Session,
    *,
    organization_id: int,
    action: str,
    outcome: str,
    subject_id: int | None = None,
    event_id: str | None = None,
    policy_version: int | None = None,
    detail: dict | None = None,
    commit: bool = False,
) -> AuditRecord:
    """Write an operational audit row. No personal fields are ever stored."""
    row = AuditRecord(
        organization_id=organization_id,
        action=action,
        outcome=outcome,
        subject_id=subject_id,
        event_id=event_id,
        policy_version=policy_version,
        detail=detail,
    )
    db.add(row)
    if commit:
        db.commit()
    return row


def erase_subject(db: Session, organization_id: int, subject_key: str) -> dict:
    """Erase a subject's personal data and derived copies in one transaction.

    - subject row anonymised (key nulled) and flagged erased (tombstone added);
    - subject_exports copies deleted;
    - materialised consent_states deleted (re-derivable, and must not return
      identifying data);
    - consent_events ledger preserved but subject_key snapshots nulled;
    - no personal field is written to the audit log.
    """
    subject = db.scalars(
        select(Subject)
        .where(
            Subject.organization_id == organization_id,
            Subject.subject_key == subject_key,
            Subject.erased.is_(False),
        )
        .with_for_update()
    ).one_or_none()
    if subject is None:
        raise NotFound("subject not found")

    deleted_at = utcnow()
    subject_id = subject.id

    db.execute(delete(SubjectExport).where(SubjectExport.subject_id == subject_id))
    db.execute(delete(ConsentState).where(ConsentState.subject_id == subject_id))

    # Strip the only identifying column from the preserved ledger.
    for event in db.scalars(
        select(ConsentEvent).where(ConsentEvent.subject_id == subject_id)
    ).all():
        event.subject_key_snapshot = None

    subject.subject_key = None
    subject.erased = True
    subject.erased_at = deleted_at

    db.add(
        SubjectDeletion(
            organization_id=organization_id, subject_id=subject_id, deleted_at=deleted_at
        )
    )
    record_audit(
        db,
        organization_id=organization_id,
        action="subject.erase",
        outcome="success",
        subject_id=subject_id,
        detail={"export_copies_deleted": True, "ledger_snapshots_nulled": True},
    )
    db.commit()
    return {"subject_key": subject_key, "erased": True, "already_erased": False, "deleted_at": deleted_at}


def create_export(
    db: Session, organization_id: int, subject_key: str, label: str, payload: dict
) -> SubjectExport:
    from app.service import get_live_subject

    subject = get_live_subject(db, organization_id, subject_key, lock=False)
    export = SubjectExport(
        organization_id=organization_id,
        subject_id=subject.id,
        label=label,
        payload=payload,
    )
    db.add(export)
    db.commit()
    db.refresh(export)
    return export


def list_exports(db: Session, organization_id: int, subject_key: str) -> list[SubjectExport]:
    from app.service import get_live_subject

    subject = get_live_subject(db, organization_id, subject_key, lock=False)
    return list(
        db.scalars(
            select(SubjectExport)
            .where(SubjectExport.subject_id == subject.id)
            .order_by(SubjectExport.id.asc())
        )
    )


def list_audit(
    db: Session,
    organization_id: int,
    *,
    limit: int = 100,
    offset: int = 0,
    outcome: str | None = None,
) -> list[AuditRecord]:
    stmt = select(AuditRecord).where(AuditRecord.organization_id == organization_id)
    if outcome is not None:
        stmt = stmt.where(AuditRecord.outcome == outcome)
    stmt = stmt.order_by(AuditRecord.id.desc()).limit(limit).offset(offset)
    return list(db.scalars(stmt))
