from __future__ import annotations

from dataclasses import dataclass

import jwt
from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AdminUser, Device
from .security import decode_admin_token, hash_device_token


def get_db(request: Request):
    session_factory = request.app.state.SessionLocal
    db = session_factory()
    try:
        yield db
    finally:
        db.close()


@dataclass
class AdminContext:
    user: AdminUser

    @property
    def is_global(self) -> bool:
        return self.user.tenant_id is None

    @property
    def tenant_id(self) -> str | None:
        return self.user.tenant_id

    def require_global(self) -> None:
        if not self.is_global:
            raise HTTPException(status_code=403, detail="global admin privileges required")

    def require_tenant(self, tenant_id: str) -> None:
        """404 (not 403) so tenant-scoped admins cannot probe other tenants' existence."""
        if self.tenant_id is not None and self.tenant_id != tenant_id:
            raise HTTPException(status_code=404, detail="tenant not found")


def get_admin(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
    request: Request = None,
) -> AdminContext:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization[len("Bearer ") :].strip()
    secret = request.app.state.settings.jwt_secret
    try:
        payload = decode_admin_token(secret=secret, token=token)
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="invalid or expired token")
    username = payload.get("sub")
    user = db.execute(
        select(AdminUser).where(AdminUser.username == username)
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=401, detail="unknown admin")
    # Token's tenant scope must still match the user's current scope.
    if payload.get("tid") != user.tenant_id:
        raise HTTPException(status_code=401, detail="stale token scope")
    return AdminContext(user=user)


def get_device(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> Device:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing device token")
    token = authorization[len("Bearer ") :].strip()
    device = db.execute(
        select(Device).where(Device.token_hash == hash_device_token(token))
    ).scalar_one_or_none()
    if device is None:
        raise HTTPException(status_code=401, detail="invalid device token")
    if device.revoked:
        raise HTTPException(status_code=401, detail="device revoked")
    return device
