"""Domain services: connect / heartbeat / disconnect / revoke / sweep.

All concurrency rules are enforced here with row locks plus the partial unique
indexes declared on the Lease model. Lock ordering is always
device -> lease -> access point, consistent across connect and revoke, which
prevents lock-order deadlocks.
"""
from __future__ import annotations

from datetime import timedelta

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import allocation
from app.config import get_settings
from app.models import (
    AccessPoint,
    Device,
    Lease,
    LeaseEvent,
    LeaseReason,
    LeaseStatus,
    utcnow,
)

settings = get_settings()

_client_lease_conflict = lambda detail: HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


def _add_event(db: Session, lease: Lease, event_type: str, reason: LeaseReason, detail: str | None = None) -> None:
    db.add(LeaseEvent(lease_id=lease.id, event_type=event_type, reason=reason, detail=detail))


def close_lease(db: Session, lease: Lease, reason: LeaseReason) -> bool:
    """Close exactly once. Caller must hold ``FOR UPDATE`` on the lease row.

    Returns True when *this* call performed the close, False if it was already
    closed (e.g. expiry racing a revoke).
    """
    if lease.status == LeaseStatus.closed:
        return False
    lease.status = LeaseStatus.closed
    lease.closed_at = utcnow()
    lease.close_reason = reason
    _add_event(db, lease, "closed", reason)
    return True


def connect(
    db: Session,
    device: Device,
    access_point_id: int,
    idempotency_key: str | None,
) -> Lease:
    # 1) Lock the device row: serializes connect/revoke for this device.
    locked_device = db.get(Device, device.id, with_for_update=True)

    if locked_device.revoked:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="device has been revoked and may not establish sessions",
        )

    # 2) Idempotency key handling: the same key + device must resolve to the
    #    same lease for repeated connect requests, while a key may never create
    #    a *new* lease once its original lease has closed.
    if idempotency_key is not None:
        keyed = db.scalar(
            select(Lease)
            .where(Lease.device_id == locked_device.id, Lease.idempotency_key == idempotency_key)
            .order_by(Lease.id.desc())
            .limit(1)
            .with_for_update()
        )
        if keyed is not None:
            if keyed.status == LeaseStatus.active:
                return keyed
            raise _client_lease_conflict("idempotency key was already used by this device")

    # 3) An existing active lease (connected without this key) still makes the
    #    connect idempotent; lock the latest lease row in the global order
    #    device -> lease -> access point to avoid lock-order deadlocks.
    latest = db.scalar(
        select(Lease)
        .where(Lease.device_id == locked_device.id)
        .order_by(Lease.id.desc())
        .limit(1)
        .with_for_update()
    )
    if latest is not None and latest.status == LeaseStatus.active:
        return latest

    ap = db.get(AccessPoint, access_point_id, with_for_update=True)
    # 4) Strict tenant isolation: never reveal another tenant's AP existence.
    if ap is None or ap.tenant_id != locked_device.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="access point not found")

    # 5) Capacity check under the AP lock.
    active_count = db.scalar(
        select(func.count(Lease.id)).where(
            Lease.access_point_id == ap.id,
            Lease.status == LeaseStatus.active,
        )
    )
    if active_count >= ap.capacity:
        raise _client_lease_conflict("access point is at capacity")

    # 6) Unique IP allocation under the AP lock; the partial unique index is
    #    the hard backstop against duplicate IPs.
    used = set(
        db.scalars(
            select(Lease.ip_address).where(
                Lease.access_point_id == ap.id,
                Lease.status == LeaseStatus.active,
            )
        )
    )
    ip = allocation.pick_free_address(ap.pool.cidr, ap.pool.reserved_ips, used)
    if ip is None:
        raise _client_lease_conflict("address pool exhausted")

    # 7) New generation for the new lease. The device lock makes max()+1 safe.
    generation = (
        db.scalar(select(func.coalesce(func.max(Lease.generation), 0)).where(Lease.device_id == locked_device.id))
        + 1
    )

    # 8) Re-lock the device row with a fresh SELECT FOR UPDATE before writing.
    #    Under READ COMMITTED this acquires the post-commit row version, so a
    #    revoke that committed while we waited on the AP lock is visible here
    #    and cannot leave a new active lease outliving it.
    recheck = db.execute(
        select(Device.revoked).where(Device.id == locked_device.id).with_for_update()
    ).one_or_none()
    if recheck is None or recheck[0]:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="device has been revoked and may not establish sessions",
        )

    now = utcnow()
    lease = Lease(
        tenant_id=locked_device.tenant_id,
        device_id=locked_device.id,
        access_point_id=ap.id,
        ip_address=ip,
        generation=generation,
        status=LeaseStatus.active,
        idempotency_key=idempotency_key,
        last_heartbeat_at=now,
        expires_at=now + timedelta(seconds=settings.heartbeat_ttl_seconds),
    )
    db.add(lease)
    db.flush()
    _add_event(db, lease, "connected", LeaseReason.connected)
    db.commit()
    db.refresh(lease)
    return lease


def _authenticate_lease_actor(db: Session, device_id: int, lease_id: int, token_generation: int) -> Lease:
    lease = db.get(Lease, lease_id, with_for_update=True)
    if lease is None or lease.device_id != device_id:
        # Do not reveal whether the lease exists.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="session not found")
    if lease.status == LeaseStatus.closed or token_generation != lease.generation:
        raise _client_lease_conflict("session generation is stale; reconnect to obtain a new session")
    return lease


def heartbeat(db: Session, device_id: int, lease_id: int, token_generation: int) -> Lease:
    lease = _authenticate_lease_actor(db, device_id, lease_id, token_generation)

    now = utcnow()
    # An expired-but-not-yet-swept lease is dead: a heartbeat cannot revive it.
    if now >= lease.expires_at:
        close_lease(db, lease, LeaseReason.expired)
        db.commit()
        raise _client_lease_conflict("session has already expired")

    lease.last_heartbeat_at = now
    lease.expires_at = now + timedelta(seconds=settings.heartbeat_ttl_seconds)
    db.commit()
    db.refresh(lease)
    return lease


def disconnect(db: Session, device_id: int, lease_id: int, token_generation: int) -> Lease:
    lease = _authenticate_lease_actor(db, device_id, lease_id, token_generation)
    close_lease(db, lease, LeaseReason.device_disconnect)
    db.commit()
    db.refresh(lease)
    return lease


def revoke_device(db: Session, device: Device) -> Device:
    locked = db.get(Device, device.id, with_for_update=True)
    locked.revoked = True
    active = db.scalar(
        select(Lease)
        .where(Lease.device_id == locked.id)
        .order_by(Lease.id.desc())
        .limit(1)
        .with_for_update()
    )
    if active is not None and active.status == LeaseStatus.active:
        close_lease(db, active, LeaseReason.revoked)
    db.commit()
    db.refresh(locked)
    return locked


def sweep_expired(db: Session, batch_size: int = 200) -> int:
    """Close all active leases whose deadline passed. Returns close count.

    Used both by the periodic background sweeper and startup recovery.
    ``SKIP LOCKED`` means in-flight heartbeats/connects are never blocked.
    """
    now = utcnow()
    ids = db.scalars(
        select(Lease.id)
        .where(Lease.status == LeaseStatus.active, Lease.expires_at <= now)
        .order_by(Lease.id)
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    ).all()

    closed = 0
    for lease_id in ids:
        lease = db.get(Lease, lease_id, with_for_update=True)
        if lease is None:
            continue
        if close_lease(db, lease, LeaseReason.expired):
            closed += 1
    if closed:
        db.commit()
    return closed
