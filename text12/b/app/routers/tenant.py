import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import leasing, service
from app.db import get_db
from app.deps import AdminPrincipal, get_current_admin
from app.models import AccessPoint, AddressPool, Device, LeaseTermination, Session as SessionModel
from app.schemas import (
    AccessPointCreate,
    AccessPointOut,
    DeviceCreate,
    DeviceEnrolledOut,
    DeviceOut,
    PoolCreate,
    PoolOut,
    SessionOut,
    TerminationOut,
)

router = APIRouter(prefix="/api/tenants", tags=["tenant-admin"])


# ---------------------------------------------------------------------------
# Address pools
# ---------------------------------------------------------------------------

@router.post("/{tenant_id}/pools", response_model=PoolOut, status_code=201)
def create_pool(
    tenant_id: uuid.UUID,
    body: PoolCreate,
    db: Session = Depends(get_db),
    admin: AdminPrincipal = Depends(get_current_admin),
) -> PoolOut:
    tenant_id = admin.require_tenant(tenant_id)
    pool = service.create_pool(
        db,
        tenant_id=tenant_id,
        name=body.name,
        cidr=body.cidr,
        reserved_addresses=body.reserved_addresses,
    )
    counts = service.pool_counts(db, pool.id)
    return PoolOut(id=pool.id, name=pool.name, cidr=pool.cidr, created_at=pool.created_at, **counts)


@router.get("/{tenant_id}/pools", response_model=list[PoolOut])
def list_pools(
    tenant_id: uuid.UUID,
    db: Session = Depends(get_db),
    admin: AdminPrincipal = Depends(get_current_admin),
) -> list[PoolOut]:
    tenant_id = admin.require_tenant(tenant_id)
    pools = db.execute(
        select(AddressPool).where(AddressPool.tenant_id == tenant_id).order_by(AddressPool.created_at)
    ).scalars().all()
    return [
        PoolOut(id=p.id, name=p.name, cidr=p.cidr, created_at=p.created_at, **service.pool_counts(db, p.id))
        for p in pools
    ]


# ---------------------------------------------------------------------------
# Access points
# ---------------------------------------------------------------------------

@router.post("/{tenant_id}/access-points", response_model=AccessPointOut, status_code=201)
def create_access_point(
    tenant_id: uuid.UUID,
    body: AccessPointCreate,
    db: Session = Depends(get_db),
    admin: AdminPrincipal = Depends(get_current_admin),
) -> AccessPointOut:
    tenant_id = admin.require_tenant(tenant_id)
    ap = service.create_access_point(
        db, tenant_id=tenant_id, name=body.name, pool_id=body.pool_id, capacity=body.capacity
    )
    return _ap_out(db, ap)


@router.get("/{tenant_id}/access-points", response_model=list[AccessPointOut])
def list_access_points(
    tenant_id: uuid.UUID,
    db: Session = Depends(get_db),
    admin: AdminPrincipal = Depends(get_current_admin),
) -> list[AccessPointOut]:
    tenant_id = admin.require_tenant(tenant_id)
    aps = db.execute(
        select(AccessPoint).where(AccessPoint.tenant_id == tenant_id).order_by(AccessPoint.created_at)
    ).scalars().all()
    return [_ap_out(db, ap) for ap in aps]


def _ap_out(db: Session, ap: AccessPoint) -> AccessPointOut:
    return AccessPointOut(
        id=ap.id,
        name=ap.name,
        pool_id=ap.pool_id,
        capacity=ap.capacity,
        active_sessions=service.active_session_count(db, ap.id),
        created_at=ap.created_at,
    )


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------

@router.post("/{tenant_id}/devices", response_model=DeviceEnrolledOut, status_code=201)
def enroll_device(
    tenant_id: uuid.UUID,
    body: DeviceCreate,
    db: Session = Depends(get_db),
    admin: AdminPrincipal = Depends(get_current_admin),
) -> DeviceEnrolledOut:
    tenant_id = admin.require_tenant(tenant_id)
    device, token = service.enroll_device(db, tenant_id=tenant_id, name=body.name)
    return DeviceEnrolledOut(
        id=device.id,
        name=device.name,
        revoked=device.revoked,
        created_at=device.created_at,
        device_token=token,
    )


@router.get("/{tenant_id}/devices", response_model=list[DeviceOut])
def list_devices(
    tenant_id: uuid.UUID,
    db: Session = Depends(get_db),
    admin: AdminPrincipal = Depends(get_current_admin),
) -> list[Device]:
    tenant_id = admin.require_tenant(tenant_id)
    return list(
        db.execute(
            select(Device).where(Device.tenant_id == tenant_id).order_by(Device.created_at)
        ).scalars()
    )


@router.post("/{tenant_id}/devices/{device_id}/revoke", response_model=DeviceOut)
def revoke_device(
    tenant_id: uuid.UUID,
    device_id: uuid.UUID,
    db: Session = Depends(get_db),
    admin: AdminPrincipal = Depends(get_current_admin),
) -> Device:
    tenant_id = admin.require_tenant(tenant_id)
    device = service.get_owned(db, Device, device_id, tenant_id)
    leasing.revoke_device(db, device_id=device.id)
    db.refresh(device)
    return device


@router.post("/{tenant_id}/devices/{device_id}/reissue-token", response_model=DeviceEnrolledOut)
def reissue_device_token(
    tenant_id: uuid.UUID,
    device_id: uuid.UUID,
    db: Session = Depends(get_db),
    admin: AdminPrincipal = Depends(get_current_admin),
) -> DeviceEnrolledOut:
    tenant_id = admin.require_tenant(tenant_id)
    device = service.get_owned(db, Device, device_id, tenant_id)
    token = leasing.reissue_device_token(db, device_id=device.id)
    db.refresh(device)
    return DeviceEnrolledOut(
        id=device.id,
        name=device.name,
        revoked=device.revoked,
        created_at=device.created_at,
        device_token=token,
    )


@router.post("/{tenant_id}/devices/{device_id}/reinstate", response_model=DeviceEnrolledOut)
def reinstate_device(
    tenant_id: uuid.UUID,
    device_id: uuid.UUID,
    db: Session = Depends(get_db),
    admin: AdminPrincipal = Depends(get_current_admin),
) -> DeviceEnrolledOut:
    tenant_id = admin.require_tenant(tenant_id)
    device = service.get_owned(db, Device, device_id, tenant_id)
    token = leasing.reinstate_device(db, device_id=device.id)
    db.refresh(device)
    return DeviceEnrolledOut(
        id=device.id,
        name=device.name,
        revoked=device.revoked,
        created_at=device.created_at,
        device_token=token,
    )


# ---------------------------------------------------------------------------
# Sessions / lease audit
# ---------------------------------------------------------------------------

@router.get("/{tenant_id}/sessions", response_model=list[SessionOut])
def list_sessions(
    tenant_id: uuid.UUID,
    device_id: uuid.UUID | None = Query(default=None),
    db: Session = Depends(get_db),
    admin: AdminPrincipal = Depends(get_current_admin),
) -> list[SessionModel]:
    tenant_id = admin.require_tenant(tenant_id)
    return service.list_sessions(db, tenant_id=tenant_id, device_id=device_id)


@router.get("/{tenant_id}/sessions/{session_id}/termination", response_model=TerminationOut)
def get_termination(
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    db: Session = Depends(get_db),
    admin: AdminPrincipal = Depends(get_current_admin),
) -> TerminationOut:
    tenant_id = admin.require_tenant(tenant_id)
    session = db.get(SessionModel, session_id)
    if session is None or session.tenant_id != tenant_id:
        from app.errors import LeaseError

        raise LeaseError("session_not_found", "session not found in this tenant", status=404)
    record = service.get_termination(db, session_id)
    if record is None:
        from app.errors import LeaseError

        raise LeaseError("not_terminated", "session is still active or has no termination record", status=404)
    return TerminationOut(
        session_id=record.session_id,
        reason=record.reason,
        detail=record.detail,
        terminated_at=record.terminated_at,
    )
