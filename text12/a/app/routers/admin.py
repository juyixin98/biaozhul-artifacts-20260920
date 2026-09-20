"""管理平面 API：超级管理员管理租户；租户管理员管理接入点、地址池、设备并查询租约。

所有查询都以当前认证租户的 id 为过滤条件，跨租户引用资源一律返回 404，
不泄露其他租户资源是否存在。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.addressing import canonical_cidr, pool_hosts, pools_overlap
from app.db import get_db
from app.deps import require_super_admin, require_tenant
from app import lease_service
from app.models import (
    AccessPoint,
    AddressPool,
    Device,
    Lease,
    LeaseEvent,
    LeaseStatus,
    Tenant,
)
from app.schemas import (
    AccessPointCreate,
    AccessPointOut,
    AddressPoolCreate,
    AddressPoolOut,
    DeviceCreate,
    DeviceOut,
    LeaseSummary,
    TenantCreate,
    TenantOut,
)
from app.security import generate_token, hash_token

router = APIRouter()


# ---------------------------------------------------------------- 平台级

@router.post("/admin/tenants", response_model=TenantOut, status_code=201,
             dependencies=[Depends(require_super_admin)], tags=["platform"])
def create_tenant(payload: TenantCreate, db: Session = Depends(get_db)) -> TenantOut:
    admin_key = generate_token("adm")
    tenant = Tenant(name=payload.name.strip(), admin_key_hash=hash_token(admin_key))
    db.add(tenant)
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(status_code=409,
                            detail={"error": "tenant_exists", "message": "租户名称已存在"})
    db.refresh(tenant)
    out = TenantOut(id=tenant.id, name=tenant.name, created_at=tenant.created_at, admin_key=admin_key)
    return out


# ---------------------------------------------------------------- 接入点

@router.post("/access-points", response_model=AccessPointOut, status_code=201, tags=["tenant"])
def create_access_point(
    payload: AccessPointCreate,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
) -> AccessPointOut:
    ap = AccessPoint(tenant_id=tenant.id, name=payload.name.strip(), capacity=payload.capacity)
    db.add(ap)
    db.commit()
    db.refresh(ap)
    return AccessPointOut(id=ap.id, tenant_id=ap.tenant_id, name=ap.name,
                          capacity=ap.capacity, active_sessions=0, created_at=ap.created_at)


@router.get("/access-points", response_model=list[AccessPointOut], tags=["tenant"])
def list_access_points(
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
) -> list[AccessPointOut]:
    rows = db.execute(
        select(AccessPoint, func.count(Lease.id))
        .outerjoin(Lease, (Lease.access_point_id == AccessPoint.id)
                   & (Lease.status == LeaseStatus.active))
        .where(AccessPoint.tenant_id == tenant.id)
        .group_by(AccessPoint.id)
        .order_by(AccessPoint.created_at, AccessPoint.id)
    ).all()
    return [
        AccessPointOut(id=ap.id, tenant_id=ap.tenant_id, name=ap.name, capacity=ap.capacity,
                       active_sessions=active, created_at=ap.created_at)
        for ap, active in rows
    ]


def _get_tenant_ap(db: Session, tenant: Tenant, ap_id) -> AccessPoint:
    ap = db.get(AccessPoint, ap_id)
    if ap is None or ap.tenant_id != tenant.id:
        raise HTTPException(status_code=404,
                            detail={"error": "access_point_not_found", "message": "接入点不存在"})
    return ap


# ---------------------------------------------------------------- 地址池

@router.post("/access-points/{ap_id}/pools", response_model=AddressPoolOut,
             status_code=201, tags=["tenant"])
def create_pool(
    ap_id: str,
    payload: AddressPoolCreate,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
) -> AddressPoolOut:
    ap = _get_tenant_ap(db, tenant, ap_id)

    # 校验保留地址数量不超过可用主机数（主机集合已排除网络/广播地址）
    try:
        hosts = pool_hosts(payload.cidr, payload.reserved_first, payload.reserved_last)
    except ValueError as exc:
        raise HTTPException(status_code=422,
                            detail={"error": "invalid_pool", "message": str(exc)})

    # 同一接入点内地址段不得重叠，否则“唯一 IP 占用”将无法按 AP 维度保证
    existing = db.execute(
        select(AddressPool.cidr).where(AddressPool.access_point_id == ap.id)
    ).scalars().all()
    for other in existing:
        if pools_overlap(payload.cidr, other):
            raise HTTPException(
                status_code=422,
                detail={"error": "pool_overlap",
                        "message": f"地址段 {payload.cidr} 与已有地址段 {canonical_cidr(other)} 重叠"})

    pool = AddressPool(
        tenant_id=tenant.id,
        access_point_id=ap.id,
        cidr=payload.cidr,
        reserved_first=payload.reserved_first,
        reserved_last=payload.reserved_last,
    )
    db.add(pool)
    db.commit()
    db.refresh(pool)
    return AddressPoolOut(id=pool.id, tenant_id=pool.tenant_id, access_point_id=pool.access_point_id,
                          cidr=pool.cidr, reserved_first=pool.reserved_first,
                          reserved_last=pool.reserved_last, assignable_count=len(hosts),
                          created_at=pool.created_at)


@router.get("/access-points/{ap_id}/pools", response_model=list[AddressPoolOut], tags=["tenant"])
def list_pools(
    ap_id: str,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
) -> list[AddressPoolOut]:
    ap = _get_tenant_ap(db, tenant, ap_id)
    pools = db.execute(
        select(AddressPool)
        .where(AddressPool.access_point_id == ap.id)
        .order_by(AddressPool.created_at, AddressPool.id)
    ).scalars().all()
    result = []
    for p in pools:
        count = len(pool_hosts(p.cidr, p.reserved_first, p.reserved_last))
        result.append(AddressPoolOut(id=p.id, tenant_id=p.tenant_id, access_point_id=p.access_point_id,
                                     cidr=p.cidr, reserved_first=p.reserved_first,
                                     reserved_last=p.reserved_last, assignable_count=count,
                                     created_at=p.created_at))
    return result


# ---------------------------------------------------------------- 设备

@router.post("/devices", response_model=DeviceOut, status_code=201, tags=["tenant"])
def create_device(
    payload: DeviceCreate,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
) -> DeviceOut:
    token = generate_token("dev")
    device = Device(tenant_id=tenant.id, name=payload.name.strip(), token_hash=hash_token(token))
    db.add(device)
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(status_code=409,
                            detail={"error": "device_exists", "message": "设备名称已存在"})
    db.refresh(device)
    return DeviceOut(id=device.id, tenant_id=device.tenant_id, name=device.name,
                     status=device.status.value, created_at=device.created_at,
                     revoked_at=device.revoked_at, token=token)


@router.get("/devices", response_model=list[DeviceOut], tags=["tenant"])
def list_devices(
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
) -> list[DeviceOut]:
    devices = db.execute(
        select(Device).where(Device.tenant_id == tenant.id)
        .order_by(Device.created_at, Device.id)
    ).scalars().all()
    return [
        DeviceOut(id=d.id, tenant_id=d.tenant_id, name=d.name, status=d.status.value,
                  created_at=d.created_at, revoked_at=d.revoked_at)
        for d in devices
    ]


def _get_tenant_device(db: Session, tenant: Tenant, device_id) -> Device:
    device = db.get(Device, device_id)
    if device is None or device.tenant_id != tenant.id:
        raise HTTPException(status_code=404,
                            detail={"error": "device_not_found", "message": "设备不存在"})
    return device


@router.post("/devices/{device_id}/revoke", response_model=DeviceOut, tags=["tenant"])
def revoke_device(
    device_id: str,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
) -> DeviceOut:
    device = _get_tenant_device(db, tenant, device_id)
    lease_service.revoke_device(device.id)
    db.refresh(device)
    return DeviceOut(id=device.id, tenant_id=device.tenant_id, name=device.name,
                     status=device.status.value, created_at=device.created_at,
                     revoked_at=device.revoked_at)


# ---------------------------------------------------------------- 租约查询

@router.get("/leases", response_model=list[LeaseSummary], tags=["tenant"])
def list_leases(
    status: LeaseStatus | None = None,
    device_id: str | None = None,
    access_point_id: str | None = None,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
) -> list[LeaseSummary]:
    stmt = select(Lease).where(Lease.tenant_id == tenant.id)
    if status is not None:
        stmt = stmt.where(Lease.status == status)
    if device_id is not None:
        # 跨租户设备过滤也不会泄露数据：整体受 tenant_id 条件约束
        stmt = stmt.where(Lease.device_id == device_id)
    if access_point_id is not None:
        stmt = stmt.where(Lease.access_point_id == access_point_id)
    stmt = stmt.order_by(Lease.connected_at.desc(), Lease.id).limit(500)
    leases = db.execute(stmt).scalars().all()
    return [_lease_summary(l) for l in leases]


def _get_tenant_lease(db: Session, tenant: Tenant, lease_id) -> Lease:
    lease = db.get(Lease, lease_id)
    if lease is None or lease.tenant_id != tenant.id:
        raise HTTPException(status_code=404,
                            detail={"error": "lease_not_found", "message": "租约不存在"})
    return lease


@router.get("/leases/{lease_id}", response_model=LeaseSummary, tags=["tenant"])
def get_lease(
    lease_id: str,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
) -> LeaseSummary:
    return _lease_summary(_get_tenant_lease(db, tenant, lease_id))


@router.get("/leases/{lease_id}/events", tags=["tenant"])
def get_lease_events(
    lease_id: str,
    db: Session = Depends(get_db),
    tenant: Tenant = Depends(require_tenant),
) -> list[dict]:
    lease = _get_tenant_lease(db, tenant, lease_id)
    events = db.execute(
        select(LeaseEvent)
        .where(LeaseEvent.lease_id == lease.id)
        .order_by(LeaseEvent.created_at, LeaseEvent.id)
    ).scalars().all()
    return [
        {
            "id": e.id,
            "event_type": e.event_type,
            "reason": e.reason,
            "detail": e.detail,
            "created_at": e.created_at.isoformat(),
        }
        for e in events
    ]


def _lease_summary(lease: Lease) -> LeaseSummary:
    return LeaseSummary(
        lease_id=lease.id,
        device_id=lease.device_id,
        access_point_id=lease.access_point_id,
        ip_address=str(lease.ip_address),
        generation=lease.generation,
        status=lease.status.value,
        connected_at=lease.connected_at,
        last_seen_at=lease.last_seen_at,
        ended_at=lease.ended_at,
        termination_reason=lease.termination_reason,
    )
