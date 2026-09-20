"""Policy publishing. Published versions are immutable at the database level."""
from __future__ import annotations

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import AuditAction, AuditLog, PolicyVersion
from app.services.errors import ConflictError


def publish_policy(
    db: Session,
    *,
    organization_id: int,
    api_key_id: int,
    version: str,
    body: str,
) -> PolicyVersion:
    policy = PolicyVersion(organization_id=organization_id, version=version, body=body)
    db.add(policy)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise ConflictError(f"policy version {version!r} already exists")

    db.add(
        AuditLog(
            organization_id=organization_id,
            api_key_id=api_key_id,
            action=AuditAction.policy_published,
            detail={"policy_version": version, "policy_id": policy.id},
        )
    )
    db.commit()
    db.refresh(policy)
    return policy


def list_policies(db: Session, *, organization_id: int) -> list[PolicyVersion]:
    return (
        db.query(PolicyVersion)
        .filter(PolicyVersion.organization_id == organization_id)
        .order_by(PolicyVersion.published_at.asc())
        .all()
    )
