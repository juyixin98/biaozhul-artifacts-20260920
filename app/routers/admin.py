from __future__ import annotations

import ipaddress

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..deps import AdminContext, get_admin, get_db
from ..models import (
    LEASE_ACTIVE,
    LEASE_RELEASED,
    AccessPoint,
    AdminUser,
    Device,
    IPv4Pool,
    Lease,
    Tenant,
)
from ..schemas import (
    AccessPointIn,
    AccessPointOut,
    AdminCreateIn,
    AdminOut,
    DeviceCreatedOut,
    DeviceIn,
    DeviceOut,
    LeaseOut,
    LoginIn,
    PoolIn,
    PoolOut,
    TenantIn,
    TenantOut,
    TerminationOut,
    TokenOut,
)
from ..security import (
    encode_admin_token,
    generate_device_token,
    hash_device_token,
    hash_password,
    verify_password,
)
from ..services import leases as lease_service

router = APIRouter(prefix="/admin", tags=["admin"])


def _get_tenant(db: Session, tenant_id: str) -> Tenant:
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="tenant not found")
    return tenant


# --- auth ---


@router.post("/login", response_model=TokenOut)
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)):
    user = db.execute(
        select(AdminUser).where(AdminUser.username == body.username)
    ).scalar_one_or_none()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="invalid credentials")
    settings = request.app.state.settings
    token = encode_admin_token(
        secret=settings.jwt_secret,
        subject=user.username,
        tenant_id=user.tenant_id,
        expiry_seconds=settings.jwt_expiry_seconds,
    )
    return TokenOut(access_token=token)


@router.get("/me", response_model=AdminOut)
def me(admin: AdminContext = Depends(get_admin)):
    u = admin.user
    return AdminOut(id=u.id, username=u.username, tenant_id=u.tenant_id)


# --- tenants (global admin) ---


@router.post("/tenants", response_model=TenantOut, status_code=201)
def create_tenant(
    body: TenantIn,
    admin: AdminContext = Depends(get_admin),
    db: Session = Depends(get_db),
):
    admin.require_global()
    tenant = Tenant(name=body.name)
    db.add(tenant)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="tenant name already exists")
    return tenant


@router.get("/tenants", response_model=list[TenantOut])
def list_tenants(admin: AdminContext = Depends(get_admin), db: Session = Depends(get_db)):
    if admin.is_global:
        return db.scalars(select(Tenant).order_by(Tenant.name)).all()
    tenant = db.get(Tenant, admin.tenant_id)
    return [tenant] if tenant else []


@router.post("/tenants/{tenant_id}/admins", response_model=AdminOut, status_code=201)
def create_tenant_admin(
    tenant_id: str,
    body: AdminCreateIn,
    admin: AdminContext = Depends(get_admin),
    db: Session = Depends(get_db),
):
    admin.require_tenant(tenant_id)
    _get_tenant(db, tenant_id)
    user = AdminUser(
        username=body.username,
        password_hash=hash_password(body.password),
        tenant_id=tenant_id,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="username already exists")
    return AdminOut(id=user.id, username=user.username, tenant_id=user.tenant_id)


# --- pools ---


def _pool_out(db: Session, pool: IPv4Pool) -> PoolOut:
    network = ipaddress.ip_network(pool.cidr)
    allocated = db.execute(
        select(func.count(Lease.id)).where(
            Lease.pool_id == pool.id, Lease.state == LEASE_ACTIVE
        )
    ).scalar_one()
    return PoolOut(
        id=pool.id,
        tenant_id=pool.tenant_id,
        name=pool.name,
        cidr=pool.cidr,
        reserved=list(pool.reserved or []),
        usable_addresses=lease_service.usable_address_count(network, list(pool.reserved or [])),
        allocated_addresses=allocated,
        created_at=pool.created_at,
    )


@router.post("/tenants/{tenant_id}/pools", response_model=PoolOut, status_code=201)
def create_pool(
    tenant_id: str,
    body: PoolIn,
    admin: AdminContext = Depends(get_admin),
    db: Session = Depends(get_db),
):
    admin.require_tenant(tenant_id)
    _get_tenant(db, tenant_id)
    network = ipaddress.ip_network(body.cidr)
    for addr in body.reserved:
        if ipaddress.ip_address(addr) not in network:
            raise HTTPException(
                status_code=422, detail=f"reserved address {addr} is outside {body.cidr}"
            )
    pool = IPv4Pool(
        tenant_id=tenant_id, name=body.name, cidr=body.cidr, reserved=body.reserved
    )
    db.add(pool)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="pool name already exists in tenant")
    return _pool_out(db, pool)


@router.get("/tenants/{tenant_id}/pools", response_model=list[PoolOut])
def list_pools(
    tenant_id: str,
    admin: AdminContext = Depends(get_admin),
    db: Session = Depends(get_db),
):
    admin.require_tenant(tenant_id)
    pools = db.scalars(
        select(IPv4Pool).where(IPv4Pool.tenant_id == tenant_id).order_by(IPv4Pool.name)
    ).all()
    return [_pool_out(db, p) for p in pools]


# --- access points ---


def _ap_out(db: Session, ap: AccessPoint) -> AccessPointOut:
    active = db.execute(
        select(func.count(Lease.id)).where(
            Lease.access_point_id == ap.id, Lease.state == LEASE_ACTIVE
        )
    ).scalar_one()
    return AccessPointOut(
        id=ap.id,
        tenant_id=ap.tenant_id,
        name=ap.name,
        pool_id=ap.pool_id,
        capacity=ap.capacity,
        active_sessions=active,
        created_at=ap.created_at,
    )


@router.post("/tenants/{tenant_id}/access-points", response_model=AccessPointOut, status_code=201)
def create_access_point(
    tenant_id: str,
    body: AccessPointIn,
    admin: AdminContext = Depends(get_admin),
    db: Session = Depends(get_db),
):
    admin.require_tenant(tenant_id)
    _get_tenant(db, tenant_id)
    pool = db.execute(
        select(IPv4Pool).where(IPv4Pool.id == body.pool_id, IPv4Pool.tenant_id == tenant_id)
    ).scalar_one_or_none()
    if pool is None:
        raise HTTPException(status_code=404, detail="pool not found in tenant")
    ap = AccessPoint(
        tenant_id=tenant_id, name=body.name, pool_id=pool.id, capacity=body.capacity
    )
    db.add(ap)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="access point name already exists in tenant")
    return _ap_out(db, ap)


@router.get("/tenants/{tenant_id}/access-points", response_model=list[AccessPointOut])
def list_access_points(
    tenant_id: str,
    admin: AdminContext = Depends(get_admin),
    db: Session = Depends(get_db),
):
    admin.require_tenant(tenant_id)
    aps = db.scalars(
        select(AccessPoint).where(AccessPoint.tenant_id == tenant_id).order_by(AccessPoint.name)
    ).all()
    return [_ap_out(db, ap) for ap in aps]


# --- devices ---


def _device_out(dev: Device) -> DeviceOut:
    return DeviceOut(
        id=dev.id,
        tenant_id=dev.tenant_id,
        name=dev.name,
        token_prefix=dev.token_prefix,
        revoked=dev.revoked,
        generation=dev.generation,
        created_at=dev.created_at,
    )


@router.post("/tenants/{tenant_id}/devices", response_model=DeviceCreatedOut, status_code=201)
def create_device(
    tenant_id: str,
    body: DeviceIn,
    admin: AdminContext = Depends(get_admin),
    db: Session = Depends(get_db),
):
    admin.require_tenant(tenant_id)
    _get_tenant(db, tenant_id)
    token = generate_device_token()
    dev = Device(
        tenant_id=tenant_id,
        name=body.name,
        token_hash=hash_device_token(token),
        token_prefix=token[:12],
    )
    db.add(dev)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="device name already exists in tenant")
    out = _device_out(dev)
    return DeviceCreatedOut(**out.model_dump(), token=token)


@router.get("/tenants/{tenant_id}/devices", response_model=list[DeviceOut])
def list_devices(
    tenant_id: str,
    admin: AdminContext = Depends(get_admin),
    db: Session = Depends(get_db),
):
    admin.require_tenant(tenant_id)
    devs = db.scalars(
        select(Device).where(Device.tenant_id == tenant_id).order_by(Device.name)
    ).all()
    return [_device_out(d) for d in devs]


@router.post("/tenants/{tenant_id}/devices/{device_id}/revoke", response_model=DeviceOut)
def revoke_device(
    tenant_id: str,
    device_id: str,
    admin: AdminContext = Depends(get_admin),
    db: Session = Depends(get_db),
):
    admin.require_tenant(tenant_id)
    dev = lease_service.revoke_device(db, device_id=device_id, tenant_id=tenant_id)
    return _device_out(dev)


# --- leases & terminations ---


@router.get("/tenants/{tenant_id}/leases", response_model=list[LeaseOut])
def list_leases(
    tenant_id: str,
    state: str | None = None,
    admin: AdminContext = Depends(get_admin),
    db: Session = Depends(get_db),
):
    admin.require_tenant(tenant_id)
    stmt = select(Lease).where(Lease.tenant_id == tenant_id).order_by(Lease.created_at)
    if state:
        stmt = stmt.where(Lease.state == state)
    return db.scalars(stmt).all()


@router.get("/tenants/{tenant_id}/terminations", response_model=list[TerminationOut])
def list_terminations(
    tenant_id: str,
    admin: AdminContext = Depends(get_admin),
    db: Session = Depends(get_db),
):
    admin.require_tenant(tenant_id)
    rows = db.scalars(
        select(Lease)
        .where(Lease.tenant_id == tenant_id, Lease.state == LEASE_RELEASED)
        .order_by(Lease.released_at)
    ).all()
    return [
        TerminationOut(
            lease_id=r.id,
            device_id=r.device_id,
            access_point_id=r.access_point_id,
            ip=r.ip,
            released_at=r.released_at,
            release_reason=r.release_reason,
        )
        for r in rows
    ]
