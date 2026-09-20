"""Idempotent bootstrap: seed the platform super-admin and an optional demo
tenant (admin + pool + AP + devices) so the demo and tests have something to
talk to."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import service
from app.config import settings
from app.models import AccessPoint, AddressPool, AdminUser, Device, Tenant
from app.security import hash_password


def seed_platform_admin(db: Session) -> AdminUser:
    admin = db.execute(
        select(AdminUser).where(AdminUser.username == settings.platform_admin_username)
    ).scalar_one_or_none()
    if admin is None:
        admin = AdminUser(
            username=settings.platform_admin_username,
            password_hash=hash_password(settings.platform_admin_password),
            tenant_id=None,
            is_platform_admin=True,
        )
        db.add(admin)
        db.commit()
        db.refresh(admin)
    return admin


def seed_demo(db: Session) -> dict:
    """Create a demo tenant with resources if it does not already exist."""
    out: dict = {}

    tenant = db.execute(select(Tenant).where(Tenant.name == "demo")).scalar_one_or_none()
    if tenant is None:
        tenant = Tenant(name="demo")
        db.add(tenant)
        db.flush()
    out["tenant_id"] = tenant.id

    admin = db.execute(
        select(AdminUser).where(AdminUser.username == "demo-admin")
    ).scalar_one_or_none()
    if admin is None:
        admin = AdminUser(
            username="demo-admin",
            password_hash=hash_password("demo-admin-pass"),
            tenant_id=tenant.id,
            is_platform_admin=False,
        )
        db.add(admin)

    pool = db.execute(
        select(AddressPool).where(AddressPool.tenant_id == tenant.id, AddressPool.name == "demo-pool")
    ).scalar_one_or_none()
    if pool is None:
        db.flush()
        pool = service.create_pool(
            db,
            tenant_id=tenant.id,
            name="demo-pool",
            cidr="10.10.0.0/24",
            reserved_addresses=["10.10.0.1", "10.10.0.2"],
        )

    ap = db.execute(
        select(AccessPoint).where(AccessPoint.tenant_id == tenant.id, AccessPoint.name == "demo-ap")
    ).scalar_one_or_none()
    if ap is None:
        ap = service.create_access_point(
            db, tenant_id=tenant.id, name="demo-ap", pool_id=pool.id, capacity=250
        )
    out["pool_id"] = pool.id
    out["ap_id"] = ap.id
    return out
