"""FastAPI wiring.

Endpoints
---------
POST /api/v1/did          two-group two-period DID (+ reference + diagnostics)
POST /api/v1/event-study  single-cohort event-time descriptive study
GET  /api/v1/runs/{id}    fetch an audited run by row id
GET  /api/v1/runs         fetch audited runs by request_id
GET  /health              liveness

A statistically *refused* result for a well-formed request still returns HTTP
200 with ``status="refused"`` and explicit failure categories, so the refusal is
a first-class, machine-readable outcome rather than a transport error. Malformed
payloads return the standard HTTP 422 validation error.
"""
from __future__ import annotations

import time
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from . import __version__
from .config import SETTINGS
from .contracts import (
    DIDRequest,
    EventStudyRequest,
    HealthResponse,
)
from .log import LOGGER
from .service import run_did, run_event
from .storage import AuditStore


def create_app(store: AuditStore | None = None) -> FastAPI:
    app = FastAPI(
        title="DID / Event-Time Panel Service",
        version=__version__,
        description="Two-group two-period DID and event-time descriptive "
        "service with identity alignment, fixed-weight balancing, object "
        "clustered SEs and explicit diagnostics/refusals.",
    )
    audit = store or AuditStore(SETTINGS.db_path)

    def _log(level: int, message: str, **extra: Any) -> None:
        LOGGER.log(level, message, extra=extra)

    def _uncertainties(response) -> list[str]:
        out = []
        if getattr(response, "pretrend", None) is not None:
            pt = response.pretrend
            if not pt.feasible:
                out.append("parallel_trends_unverifiable:" + pt.conclusion)
            elif pt.conclusion == "not_rejected":
                out.append("parallel_trends_only_not_rejected_not_proved")
        return out

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(
            status="ok",
            service=SETTINGS.service_name,
            version=__version__,
            db_path=SETTINGS.db_path,
        )

    @app.post("/api/v1/did")
    def did_endpoint(request: DIDRequest):
        started = time.perf_counter()
        _log(20, "did request received", request_id=request.request_id,
             endpoint="/api/v1/did", step="received",
             n_observations=len(request.observations))
        response = run_did(request)

        run_id = audit.record_run(
            request_id=request.request_id,
            endpoint="/api/v1/did",
            status=response.status,
            service=response.service,
            version=response.version,
            host=response.summary.get("processing_location", ""),
            request_obj=request.model_dump(mode="json"),
            response_obj=response.model_dump(mode="json"),
        )
        elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
        _log(
            20 if response.status == "ok" else 30,
            "did request completed",
            request_id=request.request_id,
            endpoint="/api/v1/did",
            step="completed",
            status=response.status,
            run_id=run_id,
            elapsed_ms=elapsed_ms,
            failures=[f.model_dump(mode="json") for f in response.failures],
            uncertainties=_uncertainties(response),
            excluded_object_ids=[e.object_id for e in response.excluded_records],
        )
        body = response.model_dump(mode="json")
        body["run_id"] = run_id
        return JSONResponse(body)

    @app.post("/api/v1/event-study")
    def event_endpoint(request: EventStudyRequest):
        started = time.perf_counter()
        _log(20, "event-study request received", request_id=request.request_id,
             endpoint="/api/v1/event-study", step="received",
             n_observations=len(request.observations))
        response = run_event(request)

        run_id = audit.record_run(
            request_id=request.request_id,
            endpoint="/api/v1/event-study",
            status=response.status,
            service=response.service,
            version=response.version,
            host=response.summary.get("processing_location", ""),
            request_obj=request.model_dump(mode="json"),
            response_obj=response.model_dump(mode="json"),
        )
        elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
        _log(
            20 if response.status == "ok" else 30,
            "event-study request completed",
            request_id=request.request_id,
            endpoint="/api/v1/event-study",
            step="completed",
            status=response.status,
            run_id=run_id,
            elapsed_ms=elapsed_ms,
            failures=[f.model_dump(mode="json") for f in response.failures],
            excluded_object_ids=[e.object_id for e in response.excluded_records],
        )
        body = response.model_dump(mode="json")
        body["run_id"] = run_id
        return JSONResponse(body)

    @app.get("/api/v1/runs/{run_id}")
    def get_run(run_id: int):
        row = audit.fetch_run(run_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"run {run_id} not found")
        return row

    @app.get("/api/v1/runs")
    def get_runs(request_id: str = Query(..., description="caller correlation id")):
        rows = audit.find_by_request_id(request_id)
        if not rows:
            raise HTTPException(
                status_code=404, detail=f"no runs for request_id {request_id!r}"
            )
        return {"request_id": request_id, "count": len(rows), "runs": rows}

    return app


app = create_app()
