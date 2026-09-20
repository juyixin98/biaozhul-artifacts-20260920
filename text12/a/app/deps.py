from __future__ import annotations

import hmac

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import Device, DeviceStatus, Tenant
from app.security import hash_token


def require_super_admin(x_admin_key: str | None = Header(default=None)) -> None:
    """平台超级管理员（创建租户等全局操作）。"""
    if x_admin_key is None or not hmac.compare_digest(x_admin_key, settings.super_admin_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "unauthorized", "message": "需要超级管理员密钥"},
        )


def require_tenant(
    x_admin_key: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> Tenant:
    """租户管理员：X-Admin-Key 为创建租户时发放的密钥，返回所属租户。"""
    if not x_admin_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "unauthorized", "message": "需要租户管理员密钥"},
        )
    tenant = db.execute(
        select(Tenant).where(Tenant.admin_key_hash == hash_token(x_admin_key))
    ).scalar_one_or_none()
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "unauthorized", "message": "管理员密钥无效"},
        )
    return tenant


def require_device(
    x_device_token: str | None = Header(default=None, alias="X-Device-Token"),
    db: Session = Depends(get_db),
) -> Device:
    """设备认证：X-Device-Token。已撤销设备的令牌立即失效。"""
    if not x_device_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "unauthorized", "message": "需要设备令牌"},
        )
    device = db.execute(
        select(Device).where(Device.token_hash == hash_token(x_device_token))
    ).scalar_one_or_none()
    if device is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "unauthorized", "message": "设备令牌无效"},
        )
    if device.status == DeviceStatus.revoked:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "device_revoked", "message": "设备已被撤销，令牌已失效"},
        )
    return device
