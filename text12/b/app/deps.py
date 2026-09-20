import uuid

import jwt
from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AdminUser, Device
from app.security import compare_token_hash, decode_access_token


class AdminPrincipal:
    def __init__(self, user: AdminUser):
        self.id = user.id
        self.username = user.username
        self.tenant_id = user.tenant_id
        self.is_platform_admin = user.is_platform_admin

    def require_tenant(self, tenant_id: uuid.UUID | None) -> uuid.UUID:
        """Authorize access to a tenant-scoped resource.

        Platform admins may target any tenant; tenant admins are confined to
        their own tenant. A missing/mismatched tenant is a 404 so other
        tenants' resources are not even disclosed as existing.
        """
        if self.is_platform_admin:
            if tenant_id is None:
                raise HTTPException(status_code=404, detail="tenant-scoped resource required")
            return tenant_id
        if tenant_id is not None and tenant_id != self.tenant_id:
            raise HTTPException(status_code=404, detail="not found")
        assert self.tenant_id is not None
        return self.tenant_id


def get_current_admin(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> AdminPrincipal:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization.split(" ", 1)[1].strip()
    try:
        payload = decode_access_token(token)
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="token expired") from None
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="invalid token") from None

    user = db.get(AdminUser, uuid.UUID(payload["sub"]))
    if user is None:
        raise HTTPException(status_code=401, detail="admin user no longer exists")
    return AdminPrincipal(user)


def require_platform_admin(admin: AdminPrincipal = Depends(get_current_admin)) -> AdminPrincipal:
    if not admin.is_platform_admin:
        raise HTTPException(status_code=403, detail="platform admin privileges required")
    return admin


def authenticate_device(
    x_device_token: str | None = Header(default=None, alias="X-Device-Token"),
    db: Session = Depends(get_db),
) -> Device:
    if not x_device_token:
        raise HTTPException(
            status_code=401,
            detail="missing X-Device-Token header",
            headers={"WWW-Authenticate": "X-Device-Token"},
        )
    # Tokens are high-entropy random strings; index by hash via a table scan of
    # the token set is avoided by storing unique hashes — look the hash up.
    from app.security import hash_token

    device = db.execute(
        select(Device).where(Device.token_hash == hash_token(x_device_token))
    ).scalar_one_or_none()
    if device is None or not compare_token_hash(x_device_token, device.token_hash or ""):
        raise HTTPException(status_code=401, detail="invalid device token")
    if device.revoked:
        raise HTTPException(status_code=403, detail="device revoked")
    return device
