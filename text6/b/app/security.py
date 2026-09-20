"""API-key authentication, role separation and organization scoping."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session, selectinload

from app.database import get_db
from app.models import ApiKey, Role


@dataclass(frozen=True)
class Principal:
    api_key_id: int
    organization_id: int
    role: Role


def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def get_principal(
    x_api_key: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> Principal:
    if not x_api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing X-API-Key header",
        )
    key = (
        db.query(ApiKey)
        .options(selectinload(ApiKey.organization))
        .filter(ApiKey.key_hash == hash_key(x_api_key), ApiKey.active.is_(True))
        .one_or_none()
    )
    if key is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid API key",
        )
    return Principal(api_key_id=key.id, organization_id=key.organization_id, role=key.role)


def require_admin(principal: Principal = Depends(get_principal)) -> Principal:
    if principal.role is not Role.admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin role required for this operation",
        )
    return principal


def require_auditor(principal: Principal = Depends(get_principal)) -> Principal:
    """Auditor-only read endpoints. Admins can read everything too."""
    return principal
