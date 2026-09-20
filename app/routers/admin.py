from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import allocation
from app.db import get_db
from app.deps import require_super_admin, resolve_tenant
from app.models import AccessPoint, AddressPool, Device, Lease, LeaseStatus, Tenant
from app.schemas import (
    AccessPointCreate,
    AccessPointOut,
    DeviceCreate,
    DeviceOut,
    LeaseOut,
    PoolCreate,
    PoolOut,
    TenantCreate,
    TenantOut,
)
from app.services import revoke_device
from app.tokens import hash_admin_key, issue_device_token

router = APIRouter(prefix="/api/v1", tags=["admin"])


# ---------- tenants (super admin) ----------

@router.post("/tenants", response_model=TenantOut, status_code=status.HTTP_201_CREATED)
def create_tenant(
    body: TenantCreate,
    db: Session = Depends(get_db),
    _: None = Depends(require_super_admin),
):
    existing = db.scalar(select(Tenant).where(Tenant.name == body.name))
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="tenant name already exists")

    admin_key = body.admin_key or f"ak_{secrets.token_urlsafe(24)}"
    key_hash = hash_admin_key(admin_key)
    if db.scalar(select(Tenant.id).where(Tenant.admin_key_hash == key_hash)):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="admin key collision, choose another")

    tenant = Tenant(name=body.name, admin_key_hash=key_hash)
    db.add(tenant)
    db.commit()
    db.refresh(tenant)
    out = TenantOut.model_validate(tenant)
    out.admin_key = admin_key  # shown exactly once
    return out


@router.get("/tenants", response_model=list[TenantOut])
def list_tenants(db: Session = Depends(get_db), _: None = Depends(require_super_admin)):
    return list(db.scalars(select(Tenant).order_by(Tenant.id)))


# ---------- pools ----------

def _pool_out(db: Session, pool: AddressPool) -> PoolOut:
    total, reserved, usable = allocation.pool_stats(pool.cidr, pool.reserved_ips)
    out = PoolOut.model_validate(pool)
    out.total_hosts = total
    out.reserved_count = reserved
    out.usable = usable
    return out


@router.post(
    "/tenants/{tenant_id}/pools",
    response_model=PoolOut,
    status_code=status.HTTP_201_CREATED,
)
def create_pool(
    tenant_id: int,
    body: PoolCreate,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(resolve_tenant),
):
    duplicate = db.scalar(
        select(AddressPool.id).where(AddressPool.tenant_id == tenant.id, AddressPool.name == body.name)
    )
    if duplicate is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="pool name already exists for tenant")

    pool = AddressPool(
        tenant_id=tenant.id, name=body.name, cidr=body.cidr, reserved_ips=body.reserved_ips
    )
    db.add(pool)
    db.commit()
    db.refresh(pool)
    return _pool_out(db, pool)


@router.get("/tenants/{tenant_id}/pools", response_model=list[PoolOut])
def list_pools(
    tenant_id: int,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(resolve_tenant),
):
    pools = list(db.scalars(select(AddressPool).where(AddressPool.tenant_id == tenant.id).order_by(AddressPool.id)))
    return [_pool_out(db, p) for p in pools]


# ---------- access points ----------

def _ap_out(db: Session, ap: AccessPoint) -> AccessPointOut:
    active = db.scalar(
        select(func.count(Lease.id)).where(
            Lease.access_point_id == ap.id, Lease.status == LeaseStatus.active
        )
    )
    out = AccessPointOut.model_validate(ap)
    out.active_leases = active
    return out


@router.post(
    "/tenants/{tenant_id}/access-points",
    response_model=AccessPointOut,
    status_code=status.HTTP_201_CREATED,
)
def create_access_point(
    tenant_id: int,
    body: AccessPointCreate,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(resolve_tenant),
):
    pool = db.get(AddressPool, body.pool_id)
    if pool is None or pool.tenant_id != tenant.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="address pool not found")

    duplicate = db.scalar(
        select(AccessPoint.id).where(AccessPoint.tenant_id == tenant.id, AccessPoint.name == body.name)
    )
    if duplicate is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="access point name already exists")

    ap = AccessPoint(tenant_id=tenant.id, pool_id=body.pool_id, name=body.name, capacity=body.capacity)
    db.add(ap)
    db.commit()
    db.refresh(ap)
    return _ap_out(db, ap)


@router.get("/tenants/{tenant_id}/access-points", response_model=list[AccessPointOut])
def list_access_points(
    tenant_id: int,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(resolve_tenant),
):
    aps = list(db.scalars(select(AccessPoint).where(AccessPoint.tenant_id == tenant.id).order_by(AccessPoint.id)))
    return [_ap_out(db, ap) for ap in aps]


# ---------- devices ----------

@router.post(
    "/tenants/{tenant_id}/devices",
    response_model=DeviceOut,
    status_code=status.HTTP_201_CREATED,
)
def create_device(
    tenant_id: int,
    body: DeviceCreate,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(resolve_tenant),
):
    duplicate = db.scalar(
        select(Device.id).where(Device.tenant_id == tenant.id, Device.name == body.name)
    )
    if duplicate is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="device name already exists")

    device = Device(tenant_id=tenant.id, name=body.name)
    db.add(device)
    db.commit()
    db.refresh(device)
    out = DeviceOut.model_validate(device)
    out.device_token = issue_device_token(device.id)  # shown exactly once
    return out


@router.get("/tenants/{tenant_id}/devices", response_model=list[DeviceOut])
def list_devices(
    tenant_id: int,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(resolve_tenant),
):
    return list(db.scalars(select(Device).where(Device.tenant_id == tenant.id).order_by(Device.id)))


@router.post("/tenants/{tenant_id}/devices/{device_id}/revoke", response_model=DeviceOut)
def revoke(
    tenant_id: int,
    device_id: int,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(resolve_tenant),
):
    device = db.get(Device, device_id)
    if device is None or device.tenant_id != tenant.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="device not found")
    revoke_device(db, device)  # idempotent: revoking again just re-asserts the state
    return DeviceOut.model_validate(device)


# ---------- leases (read-only admin view, tenant scoped) ----------

@router.get("/tenants/{tenant_id}/leases", response_model=list[LeaseOut])
def list_leases(
    tenant_id: int,
    status_filter: LeaseStatus | None = None,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(resolve_tenant),
):
    stmt = select(Lease).where(Lease.tenant_id == tenant.id).order_by(Lease.id.desc())
    if status_filter is not None:
        stmt = stmt.where(Lease.status == status_filter)
    return list(db.scalars(stmt))


@router.get("/tenants/{tenant_id}/devices/{device_id}/leases", response_model=list[LeaseOut])
def list_device_leases(
    tenant_id: int,
    device_id: int,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(resolve_tenant),
):
    device = db.get(Device, device_id)
    if device is None or device.tenant_id != tenant.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="device not found")
    return list(
        db.scalars(select(Lease).where(Lease.device_id == device.id).order_by(Lease.id.desc()))
    )
