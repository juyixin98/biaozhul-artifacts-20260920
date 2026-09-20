"""API-key authentication with role separation.

Every request is scoped to exactly one organization (the one that owns the
presented key); cross-organization access is structurally impossible because
all queries filter by the resolved organization id.
"""

from __future__ import annotations

from fastapi import Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import SessionLocal, get_db
from .errors import Forbidden, Unauthorized
from .models import ApiKey, Organization
from .security import api_key_hash, constant_time_equals
from .services import Principal


class _AuthError(Unauthorized):
    pass


def _extract_key(authorization: str | None, x_api_key: str | None) -> str:
    if x_api_key:
        return x_api_key.strip()
    if authorization:
        parts = authorization.split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
    raise _AuthError("missing API key (X-API-Key or Bearer token)")


def _dev_bootstrap_principal(request: Request, raw: str) -> Principal | None:
    """Demo/dev convenience: configured management/auditor keys map to the
    first organization.  Disabled entirely when CONSENTVAULT_ENVIRONMENT=prod.
    """
    settings = request.app.state.settings
    if settings.environment == "prod":
        return None
    db_session = SessionLocal()
    try:
        org = db_session.execute(
            select(Organization).order_by(Organization.id).limit(1)
        ).scalar_one_or_none()
        if org is None:
            return None
        if constant_time_equals(raw, settings.management_api_key):
            return Principal(organization_id=org.id, role="admin", key_id=-1)
        if constant_time_equals(raw, settings.effective_auditor_key):
            return Principal(organization_id=org.id, role="auditor", key_id=-2)
    finally:
        db_session.close()
    return None


def authenticate(
    request: Request,
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> Principal:
    raw = _extract_key(authorization, x_api_key)
    presented_hash = api_key_hash(raw)

    matched = db.execute(
        select(ApiKey).where(
            ApiKey.key_hash == presented_hash, ApiKey.active.is_(True)
        )
    ).scalar_one_or_none()
    if matched is not None:
        return Principal(
            organization_id=matched.organization_id, role=matched.role,
            key_id=matched.id,
        )

    principal = _dev_bootstrap_principal(request, raw)
    if principal is not None:
        return principal
    raise _AuthError("invalid API key")


def require_admin(principal: Principal = Depends(authenticate)) -> Principal:
    if principal.role != "admin":
        raise Forbidden("admin role required for this operation")
    return principal


