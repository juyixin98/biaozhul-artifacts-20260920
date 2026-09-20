"""Policy version publishing and listing.

Published versions are immutable by contract and protected at the database
level (triggers in the migration). Listing is available to auditors; publishing
requires the admin role.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import service
from app.db import get_db
from app.privacy import record_audit
from app.schemas import PolicyVersionOut, PublishPolicyIn
from app.security import Principal, get_principal, require_admin

router = APIRouter(prefix="/policies", tags=["policies"])


@router.post("", response_model=PolicyVersionOut, status_code=201)
def publish(
    data: PublishPolicyIn,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_admin),
) -> PolicyVersionOut:
    pv = service.publish_policy(db, principal.organization_id, data.body)
    record_audit(
        db,
        organization_id=principal.organization_id,
        action="policy.publish",
        outcome="success",
        policy_version=pv.version,
        commit=True,
    )
    return PolicyVersionOut(version=pv.version, body=pv.body, published_at=pv.published_at)


@router.get("", response_model=list[PolicyVersionOut])
def list_policies(
    db: Session = Depends(get_db),
    principal: Principal = Depends(get_principal),
) -> list[PolicyVersionOut]:
    rows = service.list_policies(db, principal.organization_id)
    return [
        PolicyVersionOut(version=r.version, body=r.body, published_at=r.published_at)
        for r in rows
    ]
