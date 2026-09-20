from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..deps import get_db, get_device
from ..models import LEASE_ACTIVE, Device, Lease
from ..schemas import (
    ConnectIn,
    ConnectOut,
    DisconnectIn,
    HeartbeatIn,
    HeartbeatOut,
    LeaseOut,
)
from ..services import leases as lease_service

router = APIRouter(prefix="/device", tags=["device"])


def _connect_out(lease: Lease, reused: bool, heartbeat_interval: int) -> ConnectOut:
    return ConnectOut(
        lease_id=lease.id,
        device_id=lease.device_id,
        access_point_id=lease.access_point_id,
        ip=lease.ip,
        generation=lease.generation,
        state=lease.state,
        reused=reused,
        heartbeat_interval_seconds=heartbeat_interval,
        expires_at=lease.expires_at,
    )


@router.post("/connect", response_model=ConnectOut)
def connect(body: ConnectIn, request: Request, db: Session = Depends(get_db), device: Device = Depends(get_device)):
    settings = request.app.state.settings
    lease, reused = lease_service.connect(
        db,
        device=device,
        access_point_id=body.access_point_id,
        idempotency_key=body.idempotency_key,
        ttl_seconds=settings.lease_ttl_seconds,
    )
    return _connect_out(lease, reused, settings.heartbeat_interval_seconds)


@router.post("/heartbeat", response_model=HeartbeatOut)
def heartbeat(body: HeartbeatIn, request: Request, db: Session = Depends(get_db), device: Device = Depends(get_device)):
    settings = request.app.state.settings
    lease = lease_service.heartbeat(
        db,
        device=device,
        lease_id=body.lease_id,
        generation=body.generation,
        ttl_seconds=settings.lease_ttl_seconds,
    )
    return HeartbeatOut(lease_id=lease.id, state=lease.state, expires_at=lease.expires_at)


@router.post("/disconnect", response_model=LeaseOut)
def disconnect(body: DisconnectIn, db: Session = Depends(get_db), device: Device = Depends(get_device)):
    return lease_service.disconnect(
        db, device=device, lease_id=body.lease_id, generation=body.generation
    )


@router.get("/session", response_model=LeaseOut | None)
def current_session(db: Session = Depends(get_db), device: Device = Depends(get_device)):
    return db.execute(
        select(Lease).where(Lease.device_id == device.id, Lease.state == LEASE_ACTIVE)
    ).scalar_one_or_none()
