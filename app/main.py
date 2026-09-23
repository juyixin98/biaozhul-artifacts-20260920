"""FastAPI application: signed multi-target tracking service.

Endpoints
---------
``GET  /health``                 unauthenticated liveness probe
``POST /sessions``               create a tracking session, returns id + HMAC secret
``POST /sessions/{id}/frames``   submit a frame (HMAC-SHA256 signed)
``GET  /sessions/{id}``          session/track state (HMAC-SHA256 signed)

Client-side signing is implemented in ``examples/client_demo.py``.
"""

from __future__ import annotations

import time

from fastapi import FastAPI, Header, HTTPException, Request, status

from . import security
from .mot.tracker import FrameOrderError
from .schemas import (
    CreateSessionIn,
    FrameIn,
    FrameOut,
    SessionOut,
)
from .service import BodyMismatchError, SessionStore, store as default_store

app = FastAPI(
    title="Multi-Target Trajectory Association",
    version="1.0.0",
    description="Constant-velocity Kalman + gated Hungarian tracking backend.",
)


def _error(status_code: int, code: str, detail: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": detail})


async def _require_signed(request: Request, store: SessionStore) -> tuple[str, bytes]:
    """Authenticate a request via HMAC-SHA256 headers.

    Returns ``(session_id, raw_body)``. Fails closed on every anomaly.
    """
    sid = request.headers.get(security.HEADER_SESSION)
    ts_raw = request.headers.get(security.HEADER_TS)
    sig = request.headers.get(security.HEADER_SIG)
    if not sid or not ts_raw or not sig:
        raise _error(
            status.HTTP_401_UNAUTHORIZED,
            "missing_credentials",
            f"headers {security.HEADER_SESSION}, {security.HEADER_TS} and "
            f"{security.HEADER_SIG} are all required",
        )
    secret = store.secret_of(sid)
    if secret is None or store.get(sid) is None:
        raise _error(status.HTTP_404_NOT_FOUND, "session_not_found", "unknown session id")
    try:
        ts = float(ts_raw)
    except ValueError:
        raise _error(
            status.HTTP_401_UNAUTHORIZED, "bad_timestamp", "timestamp must be unix seconds"
        )
    if abs(time.time() - ts) > security.ALLOWED_SKEW_SECONDS:
        raise _error(
            status.HTTP_401_UNAUTHORIZED,
            "timestamp_skew",
            f"timestamp differs from server time by more than "
            f"{security.ALLOWED_SKEW_SECONDS}s (replay protection)",
        )
    body = await request.body()
    if not security.verify_signature(request.method, request.url.path, ts_raw, body, secret, sig):
        raise _error(status.HTTP_403_FORBIDDEN, "bad_signature", "HMAC verification failed")
    return sid, body


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": app.title, "time": time.time()}


@app.post("/sessions", status_code=status.HTTP_201_CREATED)
async def create_session(payload: CreateSessionIn | None = None) -> dict:
    sid, secret = default_store.create(payload.tracker if payload else None)
    session = default_store.get(sid)
    assert session is not None
    return {
        "session_id": sid,
        "secret": secret,
        "warning": "The secret is shown exactly once at creation. Keep it server-side secret.",
        "created_at": session.created_at,
        "tracker": session.params.model_dump(),
    }


@app.post("/sessions/{session_id}/frames", response_model=FrameOut)
async def submit_frame(
    session_id: str,
    request: Request,
    x_session_id: str | None = Header(default=None, alias="X-Session-Id"),
    x_timestamp: str | None = Header(default=None, alias="X-Timestamp"),
    x_signature: str | None = Header(default=None, alias="X-Signature"),
) -> dict:
    sid, body = await _require_signed(request, default_store)
    if sid != session_id:
        raise _error(
            status.HTTP_403_FORBIDDEN,
            "session_mismatch",
            "path session id does not match the signed session id",
        )
    frame = FrameIn.model_validate_json(body)
    session = default_store.get(sid)
    assert session is not None
    try:
        return session.handle_frame(frame, security.body_sha256(body))
    except BodyMismatchError as exc:
        raise _error(status.HTTP_409_CONFLICT, "replay_body_mismatch", str(exc))
    except FrameOrderError as exc:
        raise _error(status.HTTP_409_CONFLICT, "frame_out_of_order", str(exc))


@app.get("/sessions/{session_id}", response_model=SessionOut)
async def get_session(
    session_id: str,
    request: Request,
    x_session_id: str | None = Header(default=None, alias="X-Session-Id"),
    x_timestamp: str | None = Header(default=None, alias="X-Timestamp"),
    x_signature: str | None = Header(default=None, alias="X-Signature"),
) -> dict:
    sid, _ = await _require_signed(request, default_store)
    if sid != session_id:
        raise _error(
            status.HTTP_403_FORBIDDEN,
            "session_mismatch",
            "path session id does not match the signed session id",
        )
    session = default_store.get(sid)
    assert session is not None
    state = session.state()
    return {"session_id": sid, **state}
