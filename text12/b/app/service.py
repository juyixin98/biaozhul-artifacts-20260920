"""Admin-side orchestration: pools (with address materialisation), access
points, device enrollment and tenant-scoped queries."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.errors import LeaseError
from app.models import (
    AccessPoint,
    AddressPool,
    Device,
    LeaseTermination,
    PoolAddress,
    Session as SessionModel,
)
from app.networking import expand_pool, parse_cidr
from app.security import generate_token, hash_token


def create_pool(
    db: Session,
    *,
    tenant_id: uuid.UUID,
    name: str,
    cidr: str,
    reserved_addresses: list[str],
) -> AddressPool:
    net = parse_cidr(cidr)
    canonical = str(net)

    if db.execute(
        select(AddressPool).where(AddressPool.tenant_id == tenant_id, AddressPool.name == name)
    ).scalar_one_or_none():
        raise LeaseError("pool_name_taken", "pool name already used in this tenant", status=409)

    # Reject overlap with another pool of the same tenant.
    for existing in db.execute(
        select(AddressPool).where(AddressPool.tenant_id == tenant_id)
    ).scalars():
        if net.overlaps(parse_cidr(existing.cidr)):
            raise LeaseError(
                "pool_overlaps",
                f"CIDR overlaps existing pool {existing.name} ({existing.cidr})",
                status=409,
            )

    rows = expand_pool(canonical, reserved_addresses)
    pool = AddressPool(tenant_id=tenant_id, name=name, cidr=canonical)
    db.add(pool)
    db.flush()  # assign pool.id
    for ip, kind, reserved in rows:
        db.add(PoolAddress(pool_id=pool.id, ip=ip, kind=kind, reserved=reserved, status="free"))
    db.commit()
    db.refresh(pool)
    return pool


def pool_counts(db: Session, pool_id: uuid.UUID) -> dict[str, int]:
    rows = db.execute(
        select(PoolAddress.kind, PoolAddress.reserved, PoolAddress.status, func.count())
        .where(PoolAddress.pool_id == pool_id)
        .group_by(PoolAddress.kind, PoolAddress.reserved, PoolAddress.status)
    ).all()
    total = usable = reserved = allocated = 0
    for kind, is_reserved, status, count in rows:
        total += count
        if kind == "usable":
            if is_reserved:
                reserved += count
            else:
                usable += count
                if status == "allocated":
                    allocated += count
    return {
        "total_addresses": total,
        "usable_addresses": usable,
        "reserved_addresses": reserved,
        "allocated_addresses": allocated,
    }


def create_access_point(
    db: Session, *, tenant_id: uuid.UUID, name: str, pool_id: uuid.UUID, capacity: int
) -> AccessPoint:
    pool = db.get(AddressPool, pool_id)
    if pool is None or pool.tenant_id != tenant_id:
        raise LeaseError("pool_not_found", "address pool does not exist in this tenant", status=404)
    if db.execute(
        select(AccessPoint).where(AccessPoint.tenant_id == tenant_id, AccessPoint.name == name)
    ).scalar_one_or_none():
        raise LeaseError("ap_name_taken", "access point name already used in this tenant", status=409)

    counts = pool_counts(db, pool.id)
    if capacity > counts["usable_addresses"]:
        raise LeaseError(
            "capacity_exceeds_pool",
            f"capacity {capacity} exceeds usable pool size {counts['usable_addresses']}",
            status=422,
        )
    ap = AccessPoint(tenant_id=tenant_id, name=name, pool_id=pool_id, capacity=capacity)
    db.add(ap)
    db.commit()
    db.refresh(ap)
    return ap


def enroll_device(db: Session, *, tenant_id: uuid.UUID, name: str) -> tuple[Device, str]:
    if db.execute(
        select(Device).where(Device.tenant_id == tenant_id, Device.name == name)
    ).scalar_one_or_none():
        raise LeaseError("device_name_taken", "device name already used in this tenant", status=409)
    token = generate_token()
    device = Device(tenant_id=tenant_id, name=name, token_hash=hash_token(token), revoked=False)
    db.add(device)
    db.commit()
    db.refresh(device)
    return device, token


def get_owned(db: Session, model, object_id: uuid.UUID, tenant_id: uuid.UUID):
    obj = db.get(model, object_id)
    if obj is None or obj.tenant_id != tenant_id:
        raise LeaseError("not_found", f"{model.__name__} not found in this tenant", status=404)
    return obj


def active_session_count(db: Session, ap_id: uuid.UUID) -> int:
    return db.execute(
        select(func.count())
        .select_from(SessionModel)
        .where(SessionModel.access_point_id == ap_id, SessionModel.status == "active")
    ).scalar_one()


def list_sessions(db: Session, *, tenant_id: uuid.UUID, device_id: uuid.UUID | None):
    stmt = select(SessionModel).where(SessionModel.tenant_id == tenant_id)
    if device_id is not None:
        # Ensure the device itself belongs to the tenant before filtering by it.
        device = db.get(Device, device_id)
        if device is None or device.tenant_id != tenant_id:
            raise LeaseError("device_not_found", "device not found in this tenant", status=404)
        stmt = stmt.where(SessionModel.device_id == device_id)
    return db.execute(stmt.order_by(SessionModel.connected_at.desc())).scalars().all()


def get_termination(db: Session, session_id: uuid.UUID) -> LeaseTermination | None:
    return db.execute(
        select(LeaseTermination).where(LeaseTermination.session_id == session_id)
    ).scalar_one_or_none()
