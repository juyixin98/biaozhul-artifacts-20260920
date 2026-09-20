"""Lease lifecycle: connect / heartbeat / disconnect / revoke / expiry.

Concurrency model (PostgreSQL):
- connect() serializes per-device on the device row (SELECT ... FOR UPDATE) and
  per-access-point on the access-point row, so capacity checks and IP picks are
  atomic against concurrent connects, revokes and sweeps.
- Uniqueness is enforced by the database, not by the application:
    * partial unique index (pool_id, ip) WHERE state='active'  -> no double IP
    * partial unique index (device_id)     WHERE state='active' -> one session/device
    * unique (device_id, idempotency_key)                       -> idempotent connect
- Every release is a conditional UPDATE ... WHERE state='active'; exactly one
  concurrent releaser wins, so a lease is terminated exactly once.
- Each new session bumps the device's monotonic generation; heartbeats and
  disconnects must present the current generation, so a late heartbeat from an
  old session can neither revive it nor extend the new one.
"""
from __future__ import annotations

import ipaddress
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import (
    LEASE_ACTIVE,
    LEASE_RELEASED,
    AccessPoint,
    Device,
    IPv4Pool,
    Lease,
    utcnow,
)

REASON_DISCONNECTED = "disconnected"
REASON_EXPIRED = "expired"
REASON_REVOKED = "revoked"


def _lease_expiry(now: datetime, ttl_seconds: int) -> datetime:
    return now + timedelta(seconds=ttl_seconds)


def allocate_ip(network: ipaddress.IPv4Network, reserved: set[str], used: set[str]) -> str | None:
    """First usable host address, excluding network/broadcast (via .hosts()),
    tenant-reserved addresses and currently leased addresses."""
    excluded = reserved | used
    for addr in network.hosts():
        candidate = str(addr)
        if candidate not in excluded:
            return candidate
    return None


def usable_address_count(network: ipaddress.IPv4Network, reserved: list[str]) -> int:
    if network.prefixlen >= 31:  # /31 point-to-point and /32 host routes
        base = network.num_addresses
    else:
        base = network.num_addresses - 2  # network + broadcast
    return max(0, base - len(set(reserved)))


def connect(
    db: Session,
    *,
    device: Device,
    access_point_id: str,
    idempotency_key: str,
    ttl_seconds: int,
) -> tuple[Lease, bool]:
    """Establish a session. Returns (lease, reused). Idempotent on
    (device, idempotency_key); safe under concurrent duplicates."""
    # Fast path: idempotent replay of an already-recorded request.
    existing = db.execute(
        select(Lease).where(
            Lease.device_id == device.id,
            Lease.idempotency_key == idempotency_key,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing, True

    try:
        # Serialize all session changes for this device (connect vs connect,
        # connect vs revoke) on the device row.
        dev = db.execute(
            select(Device).where(Device.id == device.id).with_for_update()
        ).scalar_one()
        if dev.revoked:
            raise HTTPException(status_code=401, detail="device revoked")

        # Re-check idempotency while holding the device lock: a concurrent
        # duplicate may have committed while we waited for the lock.
        existing = db.execute(
            select(Lease).where(
                Lease.device_id == dev.id,
                Lease.idempotency_key == idempotency_key,
            )
        ).scalar_one_or_none()
        if existing is not None:
            db.commit()
            return existing, True

        active = db.execute(
            select(Lease).where(Lease.device_id == dev.id, Lease.state == LEASE_ACTIVE)
        ).scalar_one_or_none()
        if active is not None:
            raise HTTPException(
                status_code=409,
                detail="device already has an active session; disconnect first or reuse the idempotency key",
            )

        # Capacity check is atomic: the access-point row lock serializes all
        # connects against this AP, and releases (UPDATEs) take the same rows.
        ap = db.execute(
            select(AccessPoint)
            .where(
                AccessPoint.id == access_point_id,
                AccessPoint.tenant_id == dev.tenant_id,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if ap is None:
            raise HTTPException(status_code=404, detail="access point not found")

        in_use = db.execute(
            select(func.count(Lease.id)).where(
                Lease.access_point_id == ap.id, Lease.state == LEASE_ACTIVE
            )
        ).scalar_one()
        if in_use >= ap.capacity:
            raise HTTPException(status_code=409, detail="access point at capacity")

        pool = db.execute(
            select(IPv4Pool).where(IPv4Pool.id == ap.pool_id).with_for_update()
        ).scalar_one()
        used_ips = set(
            db.scalars(
                select(Lease.ip).where(Lease.pool_id == pool.id, Lease.state == LEASE_ACTIVE)
            ).all()
        )
        network = ipaddress.ip_network(pool.cidr)
        ip = allocate_ip(network, set(pool.reserved or []), used_ips)
        if ip is None:
            raise HTTPException(status_code=409, detail="address pool exhausted")

        dev.generation += 1
        now = utcnow()
        lease = Lease(
            tenant_id=dev.tenant_id,
            access_point_id=ap.id,
            pool_id=pool.id,
            device_id=dev.id,
            ip=ip,
            generation=dev.generation,
            idempotency_key=idempotency_key,
            state=LEASE_ACTIVE,
            created_at=now,
            last_heartbeat_at=now,
            expires_at=_lease_expiry(now, ttl_seconds),
        )
        db.add(lease)
        db.commit()
        return lease, False
    except IntegrityError:
        # Backstop for races the row locks did not cover (e.g. a concurrent
        # duplicate that committed between our fast-path check and insert).
        db.rollback()
        existing = db.execute(
            select(Lease).where(
                Lease.device_id == device.id,
                Lease.idempotency_key == idempotency_key,
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing, True
        raise HTTPException(status_code=409, detail="concurrent session conflict; retry")
    except Exception:
        db.rollback()
        raise


def _load_lease_for_device(db: Session, *, device: Device, lease_id: str) -> Lease:
    lease = db.execute(
        select(Lease).where(Lease.id == lease_id).with_for_update()
    ).scalar_one_or_none()
    if lease is None or lease.device_id != device.id:
        raise HTTPException(status_code=404, detail="lease not found")
    return lease


def _release(db: Session, *, lease: Lease, reason: str, now: datetime) -> bool:
    """Conditional single-statement release; returns True iff this call won."""
    result = db.execute(
        update(Lease)
        .where(Lease.id == lease.id, Lease.state == LEASE_ACTIVE)
        .values(state=LEASE_RELEASED, released_at=now, release_reason=reason)
    )
    return result.rowcount == 1


def heartbeat(
    db: Session,
    *,
    device: Device,
    lease_id: str,
    generation: int,
    ttl_seconds: int,
) -> Lease:
    lease = _load_lease_for_device(db, device=device, lease_id=lease_id)
    if lease.generation != generation:
        # A newer session exists for this device; a late heartbeat from the old
        # one must not touch it (and this old lease stays released).
        raise HTTPException(status_code=409, detail="stale generation")
    now = utcnow()
    if lease.state != LEASE_ACTIVE:
        raise HTTPException(status_code=410, detail="session already terminated")
    if lease.expires_at <= now:
        # Lazily expire on touch so correctness never depends on the sweeper.
        _release(db, lease=lease, reason=REASON_EXPIRED, now=now)
        db.commit()
        raise HTTPException(status_code=410, detail="session expired")
    lease.last_heartbeat_at = now
    lease.expires_at = _lease_expiry(now, ttl_seconds)
    db.commit()
    return lease


def disconnect(
    db: Session,
    *,
    device: Device,
    lease_id: str,
    generation: int,
) -> Lease:
    lease = _load_lease_for_device(db, device=device, lease_id=lease_id)
    if lease.generation != generation:
        raise HTTPException(status_code=409, detail="stale generation")
    now = utcnow()
    if not _release(db, lease=lease, reason=REASON_DISCONNECTED, now=now):
        db.rollback()
        raise HTTPException(status_code=410, detail="session already terminated")
    db.commit()
    db.refresh(lease)
    return lease


def revoke_device(db: Session, *, device_id: str, tenant_id: str) -> Device:
    """Revoke a device and atomically terminate its active session."""
    dev = db.execute(
        select(Device)
        .where(Device.id == device_id, Device.tenant_id == tenant_id)
        .with_for_update()
    ).scalar_one_or_none()
    if dev is None:
        raise HTTPException(status_code=404, detail="device not found")
    dev.revoked = True
    now = utcnow()
    # Conditional UPDATE: races with disconnect/expiry release the lease once.
    db.execute(
        update(Lease)
        .where(Lease.device_id == dev.id, Lease.state == LEASE_ACTIVE)
        .values(state=LEASE_RELEASED, released_at=now, release_reason=REASON_REVOKED)
    )
    db.commit()
    return dev


def sweep_expired(db: Session, *, now: datetime | None = None) -> int:
    """Release every lease whose TTL elapsed. A single atomic UPDATE, so
    concurrent sweepers/heartbeats/disconnects cannot double-release."""
    now = now or datetime.now(timezone.utc)
    result = db.execute(
        update(Lease)
        .where(Lease.state == LEASE_ACTIVE, Lease.expires_at <= now)
        .values(state=LEASE_RELEASED, released_at=now, release_reason=REASON_EXPIRED)
    )
    db.commit()
    return result.rowcount


def run_sweep(session_factory) -> int:
    with session_factory() as db:
        return sweep_expired(db)
