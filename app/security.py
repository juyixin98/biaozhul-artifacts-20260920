"""Bearer API-key authentication with role and organization scoping."""
from __future__ import annotations

import hashlib
import hmac
import secrets

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import ApiKey


def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def generate_key() -> str:
    # ``cv_`` prefix makes the token type self-identifying in logs/examples.
    return "cv_" + secrets.token_urlsafe(32)


class Principal:
    def __init__(self, api_key: ApiKey):
        self.organization_id = api_key.organization_id
        self.role = api_key.role
        self.label = api_key.label

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def get_principal(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> Principal:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization.split(" ", 1)[1].strip()
    api_key = db.scalars(
        select(ApiKey).where(ApiKey.key_hash == hash_key(token), ApiKey.active.is_(True))
    ).one_or_none()
    if api_key is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or inactive API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return Principal(api_key)


def require_admin(principal: Principal = Depends(get_principal)) -> Principal:
    if not principal.is_admin:
        # 403: the credential is valid but the role forbids writes.
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin role required")
    return principal


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)
