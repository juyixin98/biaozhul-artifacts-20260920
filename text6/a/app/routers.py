from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from . import services
from .auth import require_admin, authenticate
from .db import get_db
from .schemas import (
    BatchImportRequest,
    BatchImportResult,
    ConsentView,
    ExportCopyCreate,
    GrantRequest,
    PolicyPublish,
    PolicyVersionOut,
    PurposeCreate,
    PurposeOut,
    RebuildResult,
    SubjectDeleteResult,
    WithdrawalRequest,
)

router = APIRouter(prefix="/api/v1", tags=["consent"])


# ------------------------------------------------------------- purposes / policies


@router.post("/purposes", response_model=PurposeOut, status_code=201)
def create_purpose(
    body: PurposeCreate,
    db: Session = Depends(get_db),
    principal: services.Principal = Depends(require_admin),
):
    return services.create_purpose(db, principal, body.key, body.description)


@router.get("/purposes", response_model=list[PurposeOut])
def list_purposes(
    db: Session = Depends(get_db),
    principal: services.Principal = Depends(authenticate),
):
    return services.list_purposes(db, principal)


@router.post("/purposes/{purpose_key}/policy-versions",
             response_model=PolicyVersionOut, status_code=201)
def publish_policy(
    purpose_key: str,
    body: PolicyPublish,
    db: Session = Depends(get_db),
    principal: services.Principal = Depends(require_admin),
):
    pv = services.publish_policy_version(db, principal, purpose_key, body.body)
    return PolicyVersionOut(
        policy_id=pv.policy_id, version=pv.version, body=pv.body,
        published_at=pv.published_at,
    )


@router.get("/purposes/{purpose_key}/policy-versions",
            response_model=list[PolicyVersionOut])
def list_policies(
    purpose_key: str,
    db: Session = Depends(get_db),
    principal: services.Principal = Depends(authenticate),
):
    out = []
    for pv in services.list_policy_versions(db, principal, purpose_key):
        out.append(
            PolicyVersionOut(policy_id=pv.policy_id, version=pv.version,
                             body=pv.body, published_at=pv.published_at)
        )
    return out


# ----------------------------------------------------------------- consent writes


@router.post("/events/grant", response_model=dict, status_code=200)
def grant(
    body: GrantRequest,
    db: Session = Depends(get_db),
    principal: services.Principal = Depends(require_admin),
):
    outcome = services.write_event(
        db,
        principal,
        event_id=body.event_id,
        expected_version=body.expected_version,
        subject_ref=body.subject_ref,
        purpose_key=body.purpose_key,
        action="grant",
        policy_version=body.policy_version,
        expires_at=body.expires_at,
    )
    return {"replayed": outcome.replayed, **outcome.response}


@router.post("/events/withdrawal", response_model=dict, status_code=200)
def withdraw(
    body: WithdrawalRequest,
    db: Session = Depends(get_db),
    principal: services.Principal = Depends(require_admin),
):
    outcome = services.write_event(
        db,
        principal,
        event_id=body.event_id,
        expected_version=body.expected_version,
        subject_ref=body.subject_ref,
        purpose_key=body.purpose_key,
        action="withdrawal",
    )
    return {"replayed": outcome.replayed, **outcome.response}


@router.post("/events/batch", response_model=BatchImportResult)
def batch_import(
    body: BatchImportRequest,
    db: Session = Depends(get_db),
    principal: services.Principal = Depends(require_admin),
):
    result = services.batch_import(db, principal, [item.model_dump() for item in body.events])
    return BatchImportResult(**result)


# ---------------------------------------------------------------- consent verification


@router.get("/verify", response_model=ConsentView)
def verify(
    subject_ref: str,
    purpose_key: str,
    db: Session = Depends(get_db),
    principal: services.Principal = Depends(authenticate),
):
    return services.verify_consent(db, principal, subject_ref, purpose_key)


@router.get("/history", response_model=list[dict])
def history(
    subject_ref: str,
    purpose_key: str,
    db: Session = Depends(get_db),
    principal: services.Principal = Depends(authenticate),
):
    rows = services.history_for(db, principal, subject_ref, purpose_key)
    return [
        {
            "sequence": e.sequence,
            "event_type": e.event_type,
            "policy_version": e.policy_version,
            "expires_at": e.expires_at,
            "occurred_at": e.occurred_at,
        }
        for e in rows
    ]


# ------------------------------------------------------------------ administration


@router.post("/rebuild", response_model=RebuildResult)
def rebuild(
    db: Session = Depends(get_db),
    principal: services.Principal = Depends(require_admin),
):
    return services.rebuild_states(db, principal)


@router.delete("/subjects/{subject_ref}", response_model=SubjectDeleteResult)
def delete_subject(
    subject_ref: str,
    db: Session = Depends(get_db),
    principal: services.Principal = Depends(require_admin),
):
    return services.delete_subject(db, principal, subject_ref)


@router.post("/subjects/{subject_ref}/export-copies",
             response_model=dict, status_code=201)
def register_export_copy(
    subject_ref: str,
    body: ExportCopyCreate,
    db: Session = Depends(get_db),
    principal: services.Principal = Depends(require_admin),
):
    copy = services.register_export_copy(db, principal, subject_ref, body.copy_label)
    return {"id": copy.id, "copy_label": copy.copy_label, "created_at": copy.created_at}


@router.get("/history-by-pseudonym", response_model=list[dict])
def history_by_pseudonym(
    pseudonym: str,
    purpose_key: str | None = None,
    db: Session = Depends(get_db),
    principal: services.Principal = Depends(authenticate),
):
    """Auditor entry point for retained, pseudonymous history after erasure.

    The pseudonym appears in the audit log of a ``subject.delete`` action; it is
    a one-way hash and never maps back to a natural person through this API.
    """
    rows = services.history_by_pseudonym(db, principal, pseudonym, purpose_key)
    return [
        {
            "subject_pseudonym": e.subject_pseudonym,
            "purpose_key": e.purpose_key,
            "sequence": e.sequence,
            "event_type": e.event_type,
            "policy_version": e.policy_version,
            "expires_at": e.expires_at,
            "occurred_at": e.occurred_at,
        }
        for e in rows
    ]


@router.get("/audit-logs", response_model=list[dict])
def audit_logs(
    limit: int = 100,
    db: Session = Depends(get_db),
    principal: services.Principal = Depends(authenticate),
):
    import json

    rows = services.list_audit_logs(db, principal, min(limit, 500))
    return [
        {
            "id": r.id,
            "organization_id": r.organization_id,
            "actor_role": r.actor_role,
            "action": r.action,
            "detail": json.loads(r.detail_json),
            "occurred_at": r.occurred_at,
        }
        for r in rows
    ]
