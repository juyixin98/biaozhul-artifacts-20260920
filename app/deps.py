from __future__ import annotations

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import Device, Tenant
from app.tokens import decode_token, hash_admin_key


def _bearer(authorization: str | None, x_token: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return x_token


def require_super_admin(
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
    authorization: str | None = Header(default=None),
    x_device_token: str | None = Header(default=None, alias="X-Device-Token"),
) -> None:
    # A device/session bearer is authenticated as the wrong principal class.
    bearer = _bearer(authorization, x_device_token)
    if bearer is not None:
        claims = decode_token(bearer)
        if claims is not None and claims.get("k") in ("device", "session"):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="super admin key required")
    if not x_admin_key or x_admin_key != get_settings().super_admin_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="super admin key required")


def resolve_tenant(
    tenant_id: int,
    db: Session = Depends(get_db),
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
    authorization: str | None = Header(default=None),
    x_device_token: str | None = Header(default=None, alias="X-Device-Token"),
) -> Tenant:
    """Dependency for /tenants/{tenant_id}/... routes.

    Super admin key is accepted for every tenant. A device bearer token is
    rejected outright (403) so devices can never drive admin endpoints.
    """
    # Device credentials are never valid admin credentials, even when an
    # (invalid) X-Admin-Key is also present.
    bearer = _bearer(authorization, x_device_token)
    if bearer is not None:
        claims = decode_token(bearer)
        if claims is not None and claims.get("k") in ("device", "session"):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin access required")

    if not x_admin_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="admin key required")

    if x_admin_key == get_settings().super_admin_key:
        tenant = db.get(Tenant, tenant_id)
        if tenant is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="tenant not found")
        return tenant

    tenant = db.scalar(select(Tenant).where(Tenant.id == tenant_id, Tenant.admin_key_hash == hash_admin_key(x_admin_key)))
    if tenant is None:
        # Either the tenant does not exist or the key is wrong: do not leak which.
        exists = db.get(Tenant, tenant_id)
        if exists is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="tenant not found")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid admin key")
    return tenant


def require_device(
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
    x_device_token: str | None = Header(default=None, alias="X-Device-Token"),
) -> Device:
    """Authenticate a device bearer token. Revoked devices are refused."""
    token = _bearer(authorization, x_device_token)
    claims = decode_token(token) if token else None
    if claims is None or claims.get("k") != "device":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="valid device token required")

    device = db.get(Device, claims["id"])
    if device is None or device.revoked:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="device not permitted")
    return device
