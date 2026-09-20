"""Consent grant/withdraw, verification and history endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app import service
from app.db import get_db
from app.errors import ConsentError
from app.privacy import record_audit
from app.schemas import ConsentOut, EventOut, GrantIn, WriteResultOut
from app.security import Principal, get_principal, require_admin

router = APIRouter(tags=["consents"])


@router.post("/consents", response_model=WriteResultOut, status_code=201)
def grant_or_withdraw(
    data: GrantIn,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_admin),
) -> WriteResultOut:
    try:
        result = service.apply_event(db, principal.organization_id, data)
    except ConsentError:
        # The attempt failed validation/conflict: nothing reached the ledger.
        # Record the operational failure with no personal payload.
        record_audit(
            db,
            organization_id=principal.organization_id,
            action=f"consent.{data.action}",
            outcome="failure",
            event_id=data.event_id,
            detail={"purpose": data.purpose},
            commit=True,
        )
        raise

    record_audit(
        db,
        organization_id=principal.organization_id,
        action=f"consent.{data.action}",
        outcome="success",
        event_id=data.event_id,
        policy_version=result.policy_version,
        detail={"replayed": result.replayed, "state_version": result.state_version},
        commit=True,
    )
    return result


@router.get("/subjects/{subject_key}/consents/{purpose}", response_model=ConsentOut)
def verify_consent(
    subject_key: str,
    purpose: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
) -> ConsentOut:
    # Read is permitted for both admin and read-only auditor; org scoping is
    # enforced by the principal's organization_id.
    return service.evaluate_consent(db, principal.organization_id, subject_key, purpose)


@router.get("/subjects/{subject_key}/events", response_model=list[EventOut])
def history(
    subject_key: str,
    purpose: str | None = Query(default=None),
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
) -> list[EventOut]:
    events = service.list_history(db, principal.organization_id, subject_key, purpose)
    return [
        EventOut(
            event_id=e.event_id,
            subject_key=e.subject_key_snapshot,
            purpose=e.purpose,
            action=e.action,
            policy_version=e.policy_version,
            expires_at=e.expires_at,
            expected_version=e.expected_version,
            state_version=e.state_version,
            created_at=e.created_at,
        )
        for e in events
    ]
