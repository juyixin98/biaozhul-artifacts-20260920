"""FastAPI application factory.

Environment:
* ``BTE_PARAM_DIR`` — directory with params.json / params.sig (default ``config``)
* ``BTE_PARAM_KEY`` — hex HMAC key; falls back to ``<param_dir>/param_key.dev.hex``
  (development only)
* ``BTE_DATA_DIR`` — session persistence directory (default ``data``)

The app refuses to start if the frozen parameter manifest fails HMAC
verification.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import __version__
from .engine import Sample, estimate
from .params import Params, load_and_verify
from .schemas import CreateSessionRequest, EstimateRequest, IngestRequest
from .state import SessionError, SessionExists, SessionNotFound, SessionStore, StaleData

DEFAULT_PARAM_DIR = Path(__file__).resolve().parent.parent / "config"


def _sanitize_jsonable(x: Any) -> Any:
    """Make a value JSON-safe: non-finite floats (NaN/Inf) are rendered as
    strings, unknown objects via repr. Never echo raw NaN into a response."""
    if isinstance(x, float):
        return x if math.isfinite(x) else f"non-finite:{x!r}"
    if isinstance(x, dict):
        return {k: _sanitize_jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_sanitize_jsonable(v) for v in x]
    if x is None or isinstance(x, (str, int, bool)):
        return x
    return repr(x)


def _load_params(param_dir: Path) -> tuple[Params, str, str]:
    key_hex = os.environ.get("BTE_PARAM_KEY")
    if key_hex is None:
        key_file = param_dir / "param_key.dev.hex"
        if not key_file.exists():
            raise RuntimeError(
                "no parameter key: set BTE_PARAM_KEY or run "
                "`python scripts/freeze_params.py` to create a dev key"
            )
        key_hex = key_file.read_text(encoding="utf-8").strip()
    signed = load_and_verify(param_dir, bytes.fromhex(key_hex))
    return Params.from_manifest(signed.manifest), signed.sha256, signed.signature


def create_app(
    data_dir: Optional[str | Path] = None,
    param_dir: Optional[str | Path] = None,
) -> FastAPI:
    param_dir = Path(param_dir or os.environ.get("BTE_PARAM_DIR", DEFAULT_PARAM_DIR))
    params, param_sha256, param_sig = _load_params(param_dir)
    data_dir = Path(data_dir or os.environ.get("BTE_DATA_DIR", "data"))
    store = SessionStore(data_dir, params, param_sha256)

    app = FastAPI(
        title="Battery Telemetry SOC Estimation (synthetic, offline)",
        version=__version__,
        description=(
            "Offline SOC estimation for synthetic battery telemetry: coulomb "
            "integration with OCV-table calibration in trusted rest windows. "
            "NOT validated for real battery control."
        ),
    )
    app.state.params = params
    app.state.store = store

    @app.exception_handler(SessionNotFound)
    async def _not_found(_: Request, exc: SessionNotFound) -> JSONResponse:
        return JSONResponse(status_code=404, content={"error": {"code": exc.code, "message": str(exc)}})

    @app.exception_handler(SessionExists)
    async def _exists(_: Request, exc: SessionExists) -> JSONResponse:
        return JSONResponse(status_code=409, content={"error": {"code": exc.code, "message": str(exc)}})

    @app.exception_handler(StaleData)
    async def _stale(_: Request, exc: StaleData) -> JSONResponse:
        return JSONResponse(status_code=409, content={"error": {
            "code": exc.code, "message": str(exc),
            "stale_timestamps": exc.stale_ts, "replay_horizon_s": exc.horizon_s,
        }})

    @app.exception_handler(SessionError)
    async def _session_err(_: Request, exc: SessionError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"error": {"code": exc.code, "message": str(exc)}})

    @app.exception_handler(RequestValidationError)
    async def _validation_err(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Sanitize detail: a NaN input would otherwise be echoed back raw,
        # which Python's JSON encoder refuses to serialize.
        return JSONResponse(status_code=422, content={
            "error": {"code": "VALIDATION_ERROR", "detail": _sanitize_jsonable(exc.errors())}
        })

    # --- meta ----------------------------------------------------------
    @app.get("/health")
    def health() -> dict:
        return {
            "status": "ok",
            "service_version": __version__,
            "param_version": params.version,
            "param_sha256": param_sha256,
            "param_signature_verified": True,
        }

    @app.get("/v1/params")
    def get_params() -> dict:
        return {
            "manifest": json.loads((param_dir / "params.json").read_text(encoding="utf-8")),
            "sha256": param_sha256,
            "signature_hmac_sha256": param_sig,
            "signature_verified": True,
        }

    # --- sessions ------------------------------------------------------
    @app.post("/v1/sessions", status_code=201)
    def create_session(req: CreateSessionRequest) -> dict:
        return store.create_session(req.session_id, req.initial_soc, req.initial_sigma)

    @app.get("/v1/sessions")
    def list_sessions() -> dict:
        return {"sessions": store.list_sessions()}

    @app.get("/v1/sessions/{session_id}")
    def get_session(session_id: str) -> dict:
        state = store.load_state(session_id)
        return {
            "session_id": session_id,
            "param_version": state["param_version"],
            "param_sha256": state["param_sha256"],
            "initial_soc": state["initial_soc"],
            "initial_sigma": state["initial_sigma"],
            "n_samples": len(state["samples"]),
            "summary": state["result"]["summary"] if state["result"] else None,
            "anchor_verified": store.verify_anchor(state),
        }

    @app.delete("/v1/sessions/{session_id}")
    def delete_session(session_id: str) -> dict:
        store.delete_session(session_id)
        return {"deleted": session_id}

    @app.post("/v1/sessions/{session_id}/samples")
    def ingest(session_id: str, req: IngestRequest) -> dict:
        samples = [Sample(s.t_s, s.current_a, s.voltage_v, s.temp_c) for s in req.samples]
        return store.ingest(session_id, samples)

    @app.get("/v1/sessions/{session_id}/soc")
    def get_soc(session_id: str) -> dict:
        state = store.load_state(session_id)
        if state["result"] is None:
            return {"session_id": session_id, "ready": False, "n_samples": 0}
        summary = state["result"]["summary"]
        last = state["result"]["trace"][-1]
        return {
            "session_id": session_id,
            "ready": True,
            "param_version": state["param_version"],
            "soc": summary["soc"],
            "sigma": summary["sigma"],
            "uncertainty": {
                "sigma_random": summary["sigma_random"],
                "sigma_bias": summary["sigma_bias"],
                "sigma_unknown": summary["sigma_unknown"],
            },
            "last_sample": {"t_s": last["t_s"], "flags": last["flags"]},
            "n_samples": summary["n_samples"],
            "n_gaps": summary["n_gaps"],
            "n_ocv_calibrations": summary["n_ocv_calibrations"],
        }

    @app.get("/v1/sessions/{session_id}/trace")
    def get_trace(
        session_id: str,
        offset: int = Query(0, ge=0),
        limit: int = Query(1000, ge=1, le=100_000),
    ) -> dict:
        state = store.load_state(session_id)
        trace = state["result"]["trace"] if state["result"] else []
        return {
            "session_id": session_id,
            "total": len(trace),
            "offset": offset,
            "limit": limit,
            "trace": trace[offset:offset + limit],
        }

    @app.get("/v1/sessions/{session_id}/events")
    def get_events(session_id: str) -> dict:
        state = store.load_state(session_id)
        return {
            "session_id": session_id,
            "events": state["result"]["events"] if state["result"] else [],
        }

    @app.get("/v1/sessions/{session_id}/evidence")
    def get_evidence(session_id: str) -> dict:
        return store.get_evidence(session_id)

    @app.post("/v1/sessions/{session_id}/recompute")
    def recompute(session_id: str) -> dict:
        return store.recompute(session_id)

    # --- stateless estimation ------------------------------------------
    @app.post("/v1/estimate")
    def estimate_stateless(req: EstimateRequest) -> dict:
        samples = sorted(
            (Sample(s.t_s, s.current_a, s.voltage_v, s.temp_c) for s in req.samples),
            key=lambda s: s.t_s,
        )
        # drop duplicate timestamps deterministically (keep first)
        deduped: list[Sample] = []
        seen: set[float] = set()
        for s in samples:
            if s.t_s in seen:
                continue
            seen.add(s.t_s)
            deduped.append(s)
        result = estimate(deduped, params, req.initial_soc, req.initial_sigma)
        result["summary"]["param_version"] = params.version
        result["param_sha256"] = param_sha256
        return result

    return app


app = create_app()
