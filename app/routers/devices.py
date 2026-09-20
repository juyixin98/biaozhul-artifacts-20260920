from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import require_device
from app.models import Device, Lease
from app.schemas import ConnectRequest, HeartbeatResponse, LeaseOut, SessionOut
from app.services import connect, disconnect, heartbeat
from app.tokens import decode_token, issue_session_token

router = APIRouter(prefix="/api/v1/sessions", tags=["devices"])


def _session_claims(x_session_token: str | None, authorization: str | None) -> dict:
    token = None
    if x_session_token:
        token = x_session_token
    elif authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    claims = decode_token(token) if token else None
    if claims is None or claims.get("k") != "session":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="valid session token required")
    return claims


def _session_out(lease: Lease) -> SessionOut:
    settings = get_settings()
    return SessionOut(
        lease=LeaseOut.model_validate(lease),
        session_token=issue_session_token(lease.id, lease.generation),
        heartbeat_interval_seconds=settings.heartbeat_interval_seconds,
    )


@router.post("", response_model=SessionOut, status_code=status.HTTP_201_CREATED)
def connect_session(
    body: ConnectRequest,
    db: Session = Depends(get_db),
    device: Device = Depends(require_device),
):
    lease = connect(db, device, body.access_point_id, body.idempotency_key)
    # Return 200 vs 201 doesn't change idempotency semantics; the lease is always usable.
    return _session_out(lease)


@router.post("/heartbeat", response_model=HeartbeatResponse)
def post_heartbeat(
    db: Session = Depends(get_db),
    device: Device = Depends(require_device),
    x_session_token: str | None = Header(default=None, alias="X-Session-Token"),
    authorization: str | None = Header(default=None),
):
    claims = _session_claims(x_session_token, authorization)
    lease = heartbeat(db, device.id, claims["id"], claims.get("g", 0))
    return HeartbeatResponse(
        lease_id=lease.id,
        generation=lease.generation,
        last_heartbeat_at=lease.last_heartbeat_at,
        expires_at=lease.expires_at,
    )


@router.post("/disconnect", response_model=LeaseOut)
def post_disconnect(
    db: Session = Depends(get_db),
    device: Device = Depends(require_device),
    x_session_token: str | None = Header(default=None, alias="X-Session-Token"),
    authorization: str | None = Header(default=None),
):
    claims = _session_claims(x_session_token, authorization)
    lease = disconnect(db, device.id, claims["id"], claims.get("g", 0))
    return LeaseOut.model_validate(lease)
