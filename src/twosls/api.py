"""FastAPI application: 2SLS estimation service.

Endpoints
---------
GET  /health                 liveness + settings snapshot (no data)
POST /api/v1/iv/estimate     run 2SLS with diagnostics
GET  /api/v1/runs/{rid}      fetch persisted evidence for one request
GET  /api/v1/runs            list recent runs

Error envelopes always look like:
    {"error": {"code", "message", "request_id", "key_state"}}
so callers can branch on ``code`` without parsing prose.
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .config import settings
from .contract import EstimationRequest
from .errors import EstimationError, TwoslsError
from .evidence import EvidenceStore
from .estimator import estimate
from .logging_utils import get_logger
from .serialize import outcome_to_dict

logger = get_logger("api")

app = FastAPI(
    title="Synthetic 2SLS Service",
    version="1.0.0",
    description="Two-stage least squares with weak-instrument / identification diagnostics.",
)

_store: EvidenceStore | None = None


def get_store() -> EvidenceStore:
    global _store
    if _store is None:
        _store = EvidenceStore(settings.database_path)
    return _store


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "status": "healthy",
        "limits": {
            "min_observations": settings.min_observations,
            "max_observations": settings.max_observations,
            "max_columns": settings.max_columns,
        },
        "thresholds": {
            "weak_f": settings.weak_f_threshold,
            "partial_r2_low": settings.partial_r2_low,
            "rank_tol": settings.rank_tol,
        },
    }


@app.post("/api/v1/iv/estimate")
def estimate_endpoint(payload: EstimationRequest) -> dict[str, Any]:
    rid = payload.request_id
    try:
        outcome = estimate(payload)
    except TwoslsError as exc:
        try:
            get_store().record_error(rid, exc.code, exc.key_state or {"reason": exc.message})
        except Exception:  # persistence must never mask the statistical error
            logger.exception("evidence persistence failed for rejected request")
        return JSONResponse(status_code=exc.http_status, content=exc.to_dict())
    except Exception:  # noqa: BLE001 - convert unknowns to a stable envelope
        logger.exception("unexpected kernel failure")
        server_err = EstimationError(
            "internal estimation failure",
            request_id=rid,
            key_state={"exception_type": "unhandled"},
        )
        return JSONResponse(status_code=500, content=server_err.to_dict())

    get_store().record_outcome(outcome)
    return outcome_to_dict(outcome)


@app.get("/api/v1/runs/{request_id}")
def get_run(request_id: str) -> dict[str, Any]:
    run = get_store().get_run(request_id)
    if run is None:
        return JSONResponse(
            status_code=404,
            content={
                "error": {
                    "code": "NOT_FOUND",
                    "message": f"no run with request_id={request_id!r}",
                    "request_id": request_id,
                    "key_state": {},
                }
            },
        )
    return run


@app.get("/api/v1/runs")
def list_runs(limit: int = 50) -> dict[str, Any]:
    limit = max(1, min(limit, 500))
    return {"runs": get_store().list_runs(limit)}


@app.middleware("http")
async def assign_request_id(request: Request, call_next):
    """Attach/echo an X-Request-ID for log correlation; no payloads logged."""
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
    response = await call_next(request)
    response.headers["x-request-id"] = rid
    return response
