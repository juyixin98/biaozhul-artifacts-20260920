"""FastAPI protocol layer for the asynchronous EKF fusion service."""

from __future__ import annotations

import asyncio
from typing import Literal

import numpy as np
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from . import __version__
from .crypto import (
    SIG_VERSION,
    canonical_message,
    hmac_verify,
    new_session_id,
    new_session_key,
)
from .ekf import validate_covariance
from .engine import DEFAULT_GATE, LATE_WINDOW_S, FusionEngine

app = FastAPI(
    title="Async EKF Fusion Service",
    version=__version__,
    description=(
        "Time-ordered 2-D position/velocity EKF fusion of odometry and GNSS "
        "measurements with checkpointed late-message replay, covariance "
        "guarding, outlier rejection and HMAC-SHA256 authentication."
    ),
)


class SessionConfig(BaseModel):
    q: float = Field(
        default=1.0,
        gt=0.0,
        description="acceleration noise spectral density per axis (m^2/s^3)",
    )
    gate: float = Field(
        default=DEFAULT_GATE,
        gt=0.0,
        description="NIS gate threshold (chi^2, 2 dof; 9.2103 = p=0.99)",
    )
    late_window_s: float = Field(
        default=LATE_WINDOW_S,
        gt=0.0,
        description="messages lagging the high-water mark by more than this "
                    "are refused",
    )
    require_hmac: bool = Field(
        default=False,
        description="when true every measurement must carry a valid v1 HMAC",
    )


class Measurement(BaseModel):
    message_id: str = Field(min_length=1, description="client-unique id")
    t: float = Field(description="measurement time in seconds (sensor clock)")
    kind: Literal["gnss", "odom"]
    z: list[float] = Field(min_length=2, max_length=2,
                           description="[x, y] or [vx, vy]")
    R: list[list[float]] = Field(
        min_length=2, max_length=2,
        description="2x2 measurement covariance, symmetric positive definite",
    )
    signature: str | None = Field(
        default=None, description="HMAC-SHA256 hex; required if session has "
                                  f"require_hmac ({SIG_VERSION} canonical form)",
    )


class Batch(BaseModel):
    measurements: list[Measurement]


class Session:
    def __init__(self, cfg: SessionConfig):
        self.config = cfg
        self.engine = FusionEngine(q=cfg.q, gate=cfg.gate,
                                   late_window_s=cfg.late_window_s)
        self.key = new_session_key()
        self.lock = asyncio.Lock()


SESSIONS: dict[str, Session] = {}


def _get_session(session_id: str) -> Session:
    s = SESSIONS.get(session_id)
    if s is None:
        raise HTTPException(status_code=404,
                            detail={"error": "session_not_found",
                                    "session_id": session_id})
    return s


def _check_measurement_input(m: Measurement) -> tuple[np.ndarray, np.ndarray] | dict:
    """Validate raw measurement payload.  Returns (z, R) or an error dict."""
    import math
    if not math.isfinite(m.t):
        return {"error": "time_non_finite", "message_id": m.message_id}
    if any(not math.isfinite(v) for v in m.z):
        return {"error": "measurement_non_finite", "message_id": m.message_id}
    if any(len(row) != 2 for row in m.R):
        return {"error": "covariance_shape", "message_id": m.message_id}
    r = np.asarray(m.R, dtype=np.float64)
    reason = validate_covariance(r)
    if reason is not None:
        return {"error": reason, "message_id": m.message_id}
    # R must be strictly positive definite: S = H P H^T + R must be
    # invertible for a real measurement update.
    min_eig = float(np.linalg.eigvalsh(0.5 * (r + r.T)).min())
    if min_eig <= 0.0:
        return {"error": "covariance_not_positive_definite",
                "message_id": m.message_id, "min_eigenvalue": min_eig}
    return np.asarray(m.z, dtype=np.float64), r


def _authenticate(session: Session, m: Measurement) -> bool:
    if not session.config.require_hmac:
        return True
    if m.signature is None:
        return False
    payload = canonical_message(m.message_id, m.t, m.kind, m.z, m.R)
    return hmac_verify(session.key, payload, m.signature)


# --------------------------------------------------------------------- routes

@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": __version__,
            "sessions": len(SESSIONS)}


@app.post("/sessions", status_code=201)
async def create_session(cfg: SessionConfig | None = None) -> dict:
    cfg = cfg or SessionConfig()
    sid = new_session_id()
    SESSIONS[sid] = Session(cfg)
    return {
        "session_id": sid,
        "hmac_key": SESSIONS[sid].key,
        "signature_version": SIG_VERSION,
        "config": cfg.model_dump(),
        "note": "hmac_key is shown once; signatures are only enforced when "
                "require_hmac=true",
    }


@app.delete("/sessions/{session_id}", status_code=200)
async def delete_session(session_id: str) -> dict:
    _get_session(session_id)
    del SESSIONS[session_id]
    return {"status": "deleted", "session_id": session_id}


@app.post("/sessions/{session_id}/measurements")
async def submit_measurement(session_id: str, m: Measurement) -> dict:
    session = _get_session(session_id)
    async with session.lock:
        if not _authenticate(session, m):
            raise HTTPException(status_code=401, detail={
                "error": "invalid_signature",
                "message_id": m.message_id,
                "expected": (
                    "hex HMAC-SHA256 of the v1 canonical message string"),
            })
        checked = _check_measurement_input(m)
        if isinstance(checked, dict):
            return {"status": "rejected", **checked}
        z, r = checked
        res = session.engine.submit(m.message_id, m.t, m.kind, z, r)
        res["message_id"] = m.message_id
        return res


@app.post("/sessions/{session_id}/measurements/batch")
async def submit_batch(session_id: str, batch: Batch) -> dict:
    session = _get_session(session_id)
    results = []
    async with session.lock:
        for m in batch.measurements:
            if not _authenticate(session, m):
                results.append({
                    "message_id": m.message_id, "status": "rejected",
                    "error": "invalid_signature", "http_hint": 401,
                })
                continue
            checked = _check_measurement_input(m)
            if isinstance(checked, dict):
                results.append({"status": "rejected", **checked})
                continue
            z, r = checked
            res = session.engine.submit(m.message_id, m.t, m.kind, z, r)
            res.setdefault("message_id", m.message_id)
            results.append(res)
    accepted = sum(1 for x in results if x.get("status") == "accepted")
    return {"results": results,
            "n": len(results), "n_accepted": accepted,
            "n_rejected": len(results) - accepted,
            "state": session.engine.state()}


@app.get("/sessions/{session_id}/state")
async def get_state(session_id: str) -> dict:
    session = _get_session(session_id)
    return session.engine.state()


@app.get("/sessions/{session_id}/steps")
async def get_steps(session_id: str, limit: int | None = None) -> dict:
    session = _get_session(session_id)
    steps = session.engine.steps_in_order()
    if limit is not None:
        steps = steps[-max(1, limit):]
    return {"steps": steps, "n": len(steps)}


@app.get("/sessions/{session_id}/rejections")
async def get_rejections(session_id: str) -> dict:
    session = _get_session(session_id)
    return {"rejections": list(session.engine.rejections.values())}


@app.get("/sessions/{session_id}/integrity")
async def get_integrity(session_id: str) -> dict:
    session = _get_session(session_id)
    report = session.engine.verify_chain()
    report["n_checkpoints"] = len(session.engine.checkpoints)
    report["n_ingested"] = len(session.engine.records)
    return report
