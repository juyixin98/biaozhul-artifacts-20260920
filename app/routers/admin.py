"""Admin endpoints: batch import, state rebuild, erasure, exports, audit log.

All endpoints are organization-scoped by the authenticated key. Writes require
the admin role; the audit log is readable by read-only auditors.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app import service
from app.db import get_db
from app.errors import ConsentError, Unprocessable
from app.privacy import (
    create_export,
    erase_subject,
    list_audit,
    list_exports,
    record_audit,
)
from app.replay import rebuild_organization
from app.schemas import (
    AuditOut,
    EraseOut,
    ExportCreateIn,
    ExportOut,
    ImportIn,
    ImportOut,
    RebuildOut,
)
from app.security import Principal, get_principal, require_admin

router = APIRouter(tags=["admin"])


@router.post("/admin/import", response_model=ImportOut)
def batch_import(
    payload: ImportIn,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_admin),
) -> ImportOut:
    if len(payload.events) > 500:
        raise Unprocessable("a batch may contain at most 500 events")

    try:
        results = service.apply_batch(db, principal.organization_id, payload.events)
    except ConsentError:
        record_audit(
            db,
            organization_id=principal.organization_id,
            action="consent.import",
            outcome="failure",
            detail={"batch_size": len(payload.events)},
            commit=True,
        )
        raise

    replayed = sum(1 for r in results if r.replayed)
    record_audit(
        db,
        organization_id=principal.organization_id,
        action="consent.import",
        outcome="success",
        detail={
            "batch_size": len(payload.events),
            "accepted": len(results),
            "replayed": replayed,
        },
        commit=True,
    )
    return ImportOut(accepted=len(results), replayed=replayed, results=results)


@router.post("/admin/rebuild", response_model=RebuildOut)
def rebuild(
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_admin),
) -> RebuildOut:
    """Recompute materialised states from the immutable ledger.

    The ledger is locked for the rebuild so concurrent writes block and then
    land safely; no event can be lost.
    """
    stats = rebuild_organization(db, principal.organization_id)
    record_audit(
        db,
        organization_id=principal.organization_id,
        action="state.rebuild",
        outcome="success",
        detail=stats,
        commit=True,
    )
    return RebuildOut(**stats)


@router.post("/subjects/{subject_key}/erase", response_model=EraseOut)
def erase(
    subject_key: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_admin),
) -> EraseOut:
    result = erase_subject(db, principal.organization_id, subject_key)
    return EraseOut(**result)


@router.post("/subjects/{subject_key}/exports", response_model=ExportOut, status_code=201)
def add_export(
    subject_key: str,
    data: ExportCreateIn,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_admin),
) -> ExportOut:
    export = create_export(db, principal.organization_id, subject_key, data.label, data.payload)
    record_audit(
        db,
        organization_id=principal.organization_id,
        action="export.create",
        outcome="success",
        subject_id=export.subject_id,
        detail={"export_id": export.id},
        commit=True,
    )
    return ExportOut(
        id=export.id, label=export.label, payload=export.payload, created_at=export.created_at
    )


@router.get("/subjects/{subject_key}/exports", response_model=list[ExportOut])
def get_exports(
    subject_key: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
) -> list[ExportOut]:
    rows = list_exports(db, principal.organization_id, subject_key)
    return [
        ExportOut(id=r.id, label=r.label, payload=r.payload, created_at=r.created_at)
        for r in rows
    ]


@router.get("/audit", response_model=list[AuditOut])
def audit_log(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    outcome: str | None = Query(default=None, pattern="^(success|failure)$"),
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
) -> list[AuditOut]:
    rows = list_audit(
        db, principal.organization_id, limit=limit, offset=offset, outcome=outcome
    )
    return [
        AuditOut(
            id=r.id,
            action=r.action,
            outcome=r.outcome,
            subject_id=r.subject_id,
            event_id=r.event_id,
            policy_version=r.policy_version,
            detail=r.detail,
            created_at=r.created_at,
        )
        for r in rows
    ]
