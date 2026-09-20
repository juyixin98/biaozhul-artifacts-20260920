"""HTTP endpoints for subjects, policies, consent, verification and audit."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import AuditLog, EventType
from app.schemas import (
    AuditOut,
    BatchImport,
    BatchResult,
    ConsentEventOut,
    ConsentVerification,
    ExportCreate,
    ExportOut,
    GrantRequest,
    PolicyOut,
    PolicyPublish,
    SubjectCreate,
    SubjectOut,
    WithdrawRequest,
)
from app.security import Principal, require_admin, require_auditor
from app.services import batch as batch_service
from app.services import consent as consent_service
from app.services import policies as policy_service
from app.services import subjects as subject_service

router = APIRouter()


# --------------------------------------------------------------------------- #
# Subjects
# --------------------------------------------------------------------------- #


@router.post("/subjects", response_model=SubjectOut, status_code=201, tags=["subjects"])
def create_subject(
    payload: SubjectCreate,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_admin),
):
    return subject_service.create_subject(
        db,
        organization_id=principal.organization_id,
        api_key_id=principal.api_key_id,
        external_ref=payload.external_ref,
        email=payload.email,
        display_name=payload.display_name,
    )


@router.get("/subjects/{subject_id}", response_model=SubjectOut, tags=["subjects"])
def get_subject(
    subject_id: int,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_auditor),
):
    return subject_service.get_subject(
        db, organization_id=principal.organization_id, subject_id=subject_id
    )


@router.get(
    "/subjects/by-ref/{external_ref:path}", response_model=SubjectOut, tags=["subjects"]
)
def resolve_subject(
    external_ref: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_auditor),
):
    return subject_service.resolve_by_ref(
        db, organization_id=principal.organization_id, external_ref=external_ref
    )


@router.post(
    "/subjects/{subject_id}/exports",
    response_model=ExportOut,
    status_code=201,
    tags=["subjects"],
)
def register_export(
    subject_id: int,
    payload: ExportCreate,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_admin),
):
    return subject_service.register_export(
        db,
        organization_id=principal.organization_id,
        api_key_id=principal.api_key_id,
        subject_id=subject_id,
        destination=payload.destination,
        payload=payload.payload,
    )


@router.post("/subjects/{subject_id}/erase", response_model=SubjectOut, tags=["subjects"])
def erase_subject(
    subject_id: int,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_admin),
):
    return subject_service.erase_subject(
        db,
        organization_id=principal.organization_id,
        api_key_id=principal.api_key_id,
        subject_id=subject_id,
    )


# --------------------------------------------------------------------------- #
# Policies
# --------------------------------------------------------------------------- #


@router.post("/policies", response_model=PolicyOut, status_code=201, tags=["policies"])
def publish_policy(
    payload: PolicyPublish,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_admin),
):
    return policy_service.publish_policy(
        db,
        organization_id=principal.organization_id,
        api_key_id=principal.api_key_id,
        version=payload.version,
        body=payload.body,
    )


@router.get("/policies", response_model=list[PolicyOut], tags=["policies"])
def list_policies(
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_auditor),
):
    return policy_service.list_policies(db, organization_id=principal.organization_id)


# --------------------------------------------------------------------------- #
# Consent events
# --------------------------------------------------------------------------- #


def _event_out(event, *, replayed: bool) -> ConsentEventOut:
    return ConsentEventOut(
        event_id=event.event_id,
        subject_id=event.subject_id,
        purpose=event.purpose,
        event_type=event.event_type.value,
        version=event.version,
        policy_version=event.policy_version.version if event.policy_version else None,
        expires_at=event.expires_at,
        created_at=event.created_at,
        replayed=replayed,
    )


@router.post("/consent/grant", response_model=ConsentEventOut, tags=["consent"])
def grant_consent(
    payload: GrantRequest,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_admin),
):
    event, replayed = consent_service.append_event(
        db,
        organization_id=principal.organization_id,
        api_key_id=principal.api_key_id,
        event_id=payload.event_id,
        subject_id=payload.subject_id,
        purpose=payload.purpose,
        expected_version=payload.expected_version,
        event_type=EventType.grant,
        policy_version=payload.policy_version,
        expires_at=payload.expires_at,
    )
    return _event_out(event, replayed=replayed)


@router.post("/consent/withdraw", response_model=ConsentEventOut, tags=["consent"])
def withdraw_consent(
    payload: WithdrawRequest,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_admin),
):
    event, replayed = consent_service.append_event(
        db,
        organization_id=principal.organization_id,
        api_key_id=principal.api_key_id,
        event_id=payload.event_id,
        subject_id=payload.subject_id,
        purpose=payload.purpose,
        expected_version=payload.expected_version,
        event_type=EventType.withdraw,
    )
    return _event_out(event, replayed=replayed)


@router.get(
    "/consent/{subject_id}/{purpose:path}/verify",
    response_model=ConsentVerification,
    tags=["consent"],
)
def verify_consent(
    subject_id: int,
    purpose: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_auditor),
):
    result = consent_service.verify_consent(
        db,
        organization_id=principal.organization_id,
        subject_id=subject_id,
        purpose=purpose,
    )
    result.pop("erased", None)
    return result


@router.get(
    "/subjects/{subject_id}/history",
    response_model=list[ConsentEventOut],
    tags=["consent"],
)
def subject_history(
    subject_id: int,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_auditor),
):
    events = consent_service.history(
        db, organization_id=principal.organization_id, subject_id=subject_id
    )
    return [_event_out(e, replayed=False) for e in events]


# --------------------------------------------------------------------------- #
# Batch import
# --------------------------------------------------------------------------- #


@router.post("/consent/batch", response_model=BatchResult, tags=["consent"])
def batch_import(
    payload: BatchImport,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_admin),
):
    return batch_service.import_batch(
        db,
        organization_id=principal.organization_id,
        api_key_id=principal.api_key_id,
        items=payload.items,
    )


# --------------------------------------------------------------------------- #
# Rebuild / audit (admin / auditor)
# --------------------------------------------------------------------------- #


@router.post("/admin/rebuild", tags=["admin"])
def rebuild_states(
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_admin),
):
    return consent_service.rebuild_states(
        db, organization_id=principal.organization_id, api_key_id=principal.api_key_id
    )


@router.get("/audit-logs", response_model=list[AuditOut], tags=["audit"])
def list_audit_logs(
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_auditor),
):
    return (
        db.query(AuditLog)
        .filter(AuditLog.organization_id == principal.organization_id)
        .order_by(AuditLog.id.desc())
        .limit(limit)
        .all()
    )
