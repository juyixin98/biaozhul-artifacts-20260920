"""Lease lifecycle: connect / heartbeat / close / revoke / sweep.

Concurrency model (PostgreSQL)
------------------------------
* ``SELECT ... FOR UPDATE`` on the device row serialises all operations for a
  device (connect / reconnect / revoke), and on the access-point row it
  serialises capacity checks for that AP. Lock order is always device -> AP,
  which is deadlock-free.
* The free address is taken with ``FOR UPDATE SKIP LOCKED``; flipping its
  status to ``allocated`` plus the partial unique-index on
  ``(pool_id, ip) WHERE status='allocated'`` guarantees an IP is handed out
  only once.
* A partial unique index ``(device_id) WHERE status='active'`` guarantees one
  active session per device even if application logic raced past the check.
* Termination is an insert into ``lease_terminations``; its unique constraint
  on ``session_id`` makes release idempotent: close, expiry and revoke can all
  fire concurrently, but the lease is released exactly once.
* Every new connection is a new *generation*. Heartbeats/close carry the
  session id + generation they belong to, so a late heartbeat for an old
  generation can never touch or extend the successor session.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.errors import LeaseError
from app.models import (
    AccessPoint,
    Device,
    LeaseTermination,
    PoolAddress,
    Session as SessionModel,
    utcnow,
)
from app.security import generate_token, hash_token

TerminationReason = Literal["client_close", "heartbeat_timeout", "revoked", "reconnect", "capacity_admin"]


@dataclass
class ConnectResult:
    session: SessionModel
    lease_token: str
    reconnected: bool
    idempotent_reused: bool


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------

def _derive_idempotent_token(session_id: uuid.UUID, idempotency_key: str) -> str:
    """Deterministic per-lease token so a retried connect replays the same
    credential. Only derivable by someone holding the server secret."""
    digest = hmac.new(
        settings.jwt_secret.encode(),
        f"lease-token:{session_id}:{idempotency_key}".encode(),
        hashlib.sha256,
    ).digest()
    return "lt_" + base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


# ---------------------------------------------------------------------------
# Internal helpers (must be called with relevant rows already locked)
# ---------------------------------------------------------------------------

def _release_once(db: Session, session: SessionModel, *, reason: TerminationReason, detail: str | None) -> bool:
    """Insert the write-once termination record and free the lease.

    Returns True when *this* call performed the release, False if the session
    had already been terminated by a concurrent actor.
    """
    savepoint = db.begin_nested()
    record = LeaseTermination(
        session_id=session.id,
        reason=reason,
        terminated_at=utcnow(),
        detail=detail,
    )
    db.add(record)
    try:
        db.flush()
    except IntegrityError:
        # Someone (close / timeout / revoke / reconnect) got there first.
        savepoint.rollback()
        return False

    session.status = "closed"
    session.closed_at = record.terminated_at
    address = db.get(PoolAddress, session.pool_address_id)
    if address is not None and address.status == "allocated":
        address.status = "free"
    db.flush()
    return True


def _next_generation(db: Session, device_id: uuid.UUID) -> int:
    current = db.execute(
        select(func.coalesce(func.max(SessionModel.generation), 0)).where(
            SessionModel.device_id == device_id
        )
    ).scalar_one()
    return int(current) + 1


def _allocate_address(db: Session, ap: AccessPoint) -> PoolAddress:
    """Atomically reserve one usable, non-reserved free address in AP's pool."""
    stmt = (
        select(PoolAddress)
        .where(
            PoolAddress.pool_id == ap.pool_id,
            PoolAddress.kind == "usable",
            PoolAddress.reserved.is_(False),
            PoolAddress.status == "free",
        )
        .order_by(PoolAddress.ip.asc())
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    address = db.execute(stmt).scalar_one_or_none()
    if address is None:
        raise LeaseError("address_pool_exhausted", "no free addresses in pool", status=507)
    address.status = "allocated"
    db.flush()
    return address


# ---------------------------------------------------------------------------
# Connect / reconnect
# ---------------------------------------------------------------------------

def connect(
    db: Session,
    *,
    device_id: uuid.UUID,
    access_point_id: uuid.UUID,
    idempotency_key: str | None,
) -> ConnectResult:
    # 1. Lock the device row: serialises connect/reconnect/revoke for it.
    device = db.execute(
        select(Device).where(Device.id == device_id).with_for_update()
    ).scalar_one_or_none()
    if device is None:
        raise LeaseError("device_not_found", "device does not exist", status=404)
    if device.revoked:
        raise LeaseError("device_revoked", "device has been revoked", status=403)

    # 2. Lock the AP (lock order: device -> AP) and enforce tenant isolation.
    ap = db.execute(
        select(AccessPoint).where(AccessPoint.id == access_point_id).with_for_update()
    ).scalar_one_or_none()
    if ap is None:
        raise LeaseError("access_point_not_found", "access point does not exist", status=404)
    if ap.tenant_id != device.tenant_id:
        raise LeaseError("cross_tenant", "access point belongs to another tenant", status=403)

    # 3. Existing active session?
    active = db.execute(
        select(SessionModel)
        .where(SessionModel.device_id == device.id, SessionModel.status == "active")
        .with_for_update()
    ).scalar_one_or_none()

    reconnected = active is not None
    if active is not None:
        # Same idempotency key + same AP -> idempotent replay of the connect.
        if (
            idempotency_key
            and active.idempotency_key == idempotency_key
            and active.access_point_id == ap.id
        ):
            token = _derive_idempotent_token(active.id, idempotency_key)
            db.commit()
            return ConnectResult(active, token, reconnected=False, idempotent_reused=True)
        # Otherwise the request is a reconnect. The old generation is torn
        # down *after* a new lease is secured below, so a failed new connect
        # leaves the old session intact.

    # 4. Capacity check under the AP lock. This device's own current session
    #    (same-AP reconnect) is excluded: it frees the slot it occupies.
    active_count = db.execute(
        select(func.count())
        .select_from(SessionModel)
        .where(
            SessionModel.access_point_id == ap.id,
            SessionModel.status == "active",
            SessionModel.device_id != device.id,
        )
    ).scalar_one()
    if active_count >= ap.capacity:
        db.rollback()
        raise LeaseError("capacity_exceeded", "access point is at full capacity", status=503)

    # 5. Atomically allocate a unique IP (old lease, if any, is still held, so
    #    the successor is guaranteed to get a distinct address).
    address = _allocate_address(db, ap)

    # 6. Terminate the old generation, then insert the new one.
    if active is not None:
        _release_once(db, active, reason="reconnect", detail="superseded by new connection")

    generation = _next_generation(db, device.id)
    session_id = uuid.uuid4()
    lease_token = (
        _derive_idempotent_token(session_id, idempotency_key)
        if idempotency_key
        else generate_token()
    )
    session = SessionModel(
        id=session_id,
        tenant_id=device.tenant_id,
        device_id=device.id,
        access_point_id=ap.id,
        pool_address_id=address.id,
        generation=generation,
        status="active",
        lease_token_hash=hash_token(lease_token),
        idempotency_key=idempotency_key,
        ip=str(address.ip),
        connected_at=utcnow(),
        last_heartbeat_at=utcnow(),
    )
    db.add(session)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        # Partial unique index on active device session, or double-allocated IP.
        raise LeaseError("lease_conflict", f"lease could not be established: {exc.orig}", status=409)
    return ConnectResult(session, lease_token, reconnected=reconnected, idempotent_reused=False)


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------

def heartbeat(
    db: Session,
    *,
    device_id: uuid.UUID,
    session_id: uuid.UUID,
    generation: int,
    lease_token: str,
) -> SessionModel:
    # Lock the device so revoke/reconnect cannot interleave with the beat.
    device = db.execute(
        select(Device).where(Device.id == device_id).with_for_update()
    ).scalar_one_or_none()
    if device is None:
        raise LeaseError("device_not_found", "device does not exist", status=404)

    session = db.execute(
        select(SessionModel).where(SessionModel.id == session_id).with_for_update()
    ).scalar_one_or_none()
    if session is None or session.device_id != device.id:
        raise LeaseError("session_not_found", "session does not exist for this device", status=404)

    # The lease token authenticates this exact generation of the lease.
    if not hmac.compare_digest(session.lease_token_hash, hash_token(lease_token)):
        raise LeaseError("invalid_lease_token", "lease token does not match", status=403)
    if session.generation != generation:
        # Late beat from a superseded generation: must never revive/extend the
        # successor session or the old one.
        raise LeaseError("stale_generation", "session generation is no longer current", status=409)
    if session.status != "active":
        raise LeaseError("lease_closed", "session has already been terminated", status=409)

    session.last_heartbeat_at = utcnow()
    db.commit()
    return session


# ---------------------------------------------------------------------------
# Client-initiated close
# ---------------------------------------------------------------------------

def close_session(
    db: Session,
    *,
    device_id: uuid.UUID,
    session_id: uuid.UUID,
    generation: int,
    lease_token: str,
) -> SessionModel:
    device = db.execute(
        select(Device).where(Device.id == device_id).with_for_update()
    ).scalar_one_or_none()
    if device is None:
        raise LeaseError("device_not_found", "device does not exist", status=404)

    session = db.execute(
        select(SessionModel).where(SessionModel.id == session_id).with_for_update()
    ).scalar_one_or_none()
    if session is None or session.device_id != device.id:
        raise LeaseError("session_not_found", "session does not exist for this device", status=404)
    if not hmac.compare_digest(session.lease_token_hash, hash_token(lease_token)):
        raise LeaseError("invalid_lease_token", "lease token does not match", status=403)
    if session.generation != generation:
        raise LeaseError("stale_generation", "session generation is no longer current", status=409)

    already_closed = session.status != "active"
    if not already_closed:
        _release_once(db, session, reason="client_close", detail="closed by device")
    db.commit()
    return session


# ---------------------------------------------------------------------------
# Revocation / token rotation
# ---------------------------------------------------------------------------

def revoke_device(db: Session, *, device_id: uuid.UUID) -> bool:
    """Revoke a device immediately: rotate its credential and tear down its
    active lease. Idempotent. Returns True when this call performed the
    revocation."""
    device = db.execute(
        select(Device).where(Device.id == device_id).with_for_update()
    ).scalar_one_or_none()
    if device is None:
        raise LeaseError("device_not_found", "device does not exist", status=404)

    active = db.execute(
        select(SessionModel)
        .where(SessionModel.device_id == device.id, SessionModel.status == "active")
        .with_for_update()
    ).scalar_one_or_none()
    if active is not None:
        _release_once(db, active, reason="revoked", detail="device revoked by admin")

    if device.revoked:
        db.commit()
        return False
    device.revoked = True
    # Rotate the credential to an unusable random value; the old device token
    # can no longer authenticate a reconnect.
    device.token_hash = hash_token(generate_token())
    db.commit()
    return True


def reissue_device_token(db: Session, *, device_id: uuid.UUID) -> str:
    device = db.execute(
        select(Device).where(Device.id == device_id).with_for_update()
    ).scalar_one_or_none()
    if device is None:
        raise LeaseError("device_not_found", "device does not exist", status=404)
    if device.revoked:
        raise LeaseError("device_revoked", "re-enroll a revoked device before issuing a token", status=409)
    token = generate_token()
    device.token_hash = hash_token(token)
    db.commit()
    return token


def reinstate_device(db: Session, *, device_id: uuid.UUID) -> str:
    """Un-revoke a device and return a freshly enrolled credential."""
    device = db.execute(
        select(Device).where(Device.id == device_id).with_for_update()
    ).scalar_one_or_none()
    if device is None:
        raise LeaseError("device_not_found", "device does not exist", status=404)
    token = generate_token()
    device.revoked = False
    device.token_hash = hash_token(token)
    db.commit()
    return token


# ---------------------------------------------------------------------------
# Lease expiry sweeper (also the restart-recovery path)
# ---------------------------------------------------------------------------

def sweep_expired_sessions(db: Session, *, timeout_seconds: int, now: dt.datetime | None = None) -> int:
    """Release active leases whose last heartbeat is older than the timeout.

    Candidates are locked SKIP LOCKED so an in-flight heartbeat/close is never
    blocked; each release is still idempotent via lease_terminations.
    """
    now = now or utcnow()
    cutoff = now - dt.timedelta(seconds=timeout_seconds)
    stale = db.execute(
        select(SessionModel)
        .where(SessionModel.status == "active", SessionModel.last_heartbeat_at < cutoff)
        .with_for_update(skip_locked=True)
    ).scalars().all()

    released = 0
    for session in stale:
        if _release_once(
            db,
            session,
            reason="heartbeat_timeout",
            detail=f"no heartbeat within {timeout_seconds}s",
        ):
            released += 1
    if released:
        db.commit()
    return released
