from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app import leasing
from app.db import get_db
from app.deps import authenticate_device
from app.models import Device
from app.schemas import CloseRequest, ConnectRequest, ConnectResponse, HeartbeatRequest, SessionOut

router = APIRouter(prefix="/api/device", tags=["device"])


def require_lease_token(x_lease_token: str | None = Header(default=None, alias="X-Lease-Token")) -> str:
    """Per-lease secret returned by /connect; scopes heartbeat/close to one
    specific generation of a device's lease."""
    if not x_lease_token:
        raise HTTPException(status_code=401, detail="missing X-Lease-Token header")
    return x_lease_token


def _session_out(s) -> SessionOut:
    return SessionOut(
        id=s.id,
        generation=s.generation,
        status=s.status,
        ip=str(s.ip),
        access_point_id=s.access_point_id,
        device_id=s.device_id,
        connected_at=s.connected_at,
        last_heartbeat_at=s.last_heartbeat_at,
        closed_at=s.closed_at,
    )


@router.post("/connect", response_model=ConnectResponse, status_code=201)
def connect(
    body: ConnectRequest,
    db: Session = Depends(get_db),
    device: Device = Depends(authenticate_device),
) -> ConnectResponse:
    result = leasing.connect(
        db,
        device_id=device.id,
        access_point_id=body.access_point_id,
        idempotency_key=body.idempotency_key,
    )
    return ConnectResponse(
        session=_session_out(result.session),
        lease_token=result.lease_token,
        reconnected=result.reconnected,
        idempotent_reused=result.idempotent_reused,
    )


@router.post("/heartbeat", response_model=SessionOut)
def heartbeat(
    body: HeartbeatRequest,
    db: Session = Depends(get_db),
    device: Device = Depends(authenticate_device),
    lease_token: str = Depends(require_lease_token),
) -> SessionOut:
    session = leasing.heartbeat(
        db,
        device_id=device.id,
        session_id=body.session_id,
        generation=body.generation,
        lease_token=lease_token,
    )
    return _session_out(session)


@router.post("/close", response_model=SessionOut)
def close(
    body: CloseRequest,
    db: Session = Depends(get_db),
    device: Device = Depends(authenticate_device),
    lease_token: str = Depends(require_lease_token),
) -> SessionOut:
    session = leasing.close_session(
        db,
        device_id=device.id,
        session_id=body.session_id,
        generation=body.generation,
        lease_token=lease_token,
    )
    return _session_out(session)
