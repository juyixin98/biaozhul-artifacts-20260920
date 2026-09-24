"""FastAPI application: offline constrained 2D trajectory smoothing."""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from . import __version__
from .schemas import SmoothRequest, SmoothResponse, STATUS_VALUES
from .service import run_smoothing, save_run_record
from .smoother import MAX_POINTS_HARD_LIMIT

logger = logging.getLogger("traj_smooth")

app = FastAPI(
    title="Constrained Trajectory Smoothing",
    version=__version__,
    description=(
        "Offline 2D polyline smoothing with curvature, deviation, edge-length "
        "and collision constraints. A new path is returned only after dense "
        "whole-trajectory collision verification; otherwise the original path "
        "and a failure status come back."
    ),
)


@app.get("/health")
def health():
    return {"status": "ok", "service": "constrained-trajectory-smoothing",
            "version": __version__, "max_points": MAX_POINTS_HARD_LIMIT}


@app.get("/api/v1/status-values")
def status_values():
    return {"status_values": list(STATUS_VALUES)}


@app.post("/api/v1/smooth", response_model=SmoothResponse)
async def smooth_endpoint(http_request: Request):
    try:
        raw = await http_request.json()
        request = SmoothRequest.model_validate(raw)
    except (ValueError, ValidationError) as exc:
        detail = exc.errors(include_url=False) if isinstance(exc, ValidationError) else [
            {"msg": "request body must be a JSON object", "input": None}
        ]
        # ctx may carry non-JSON-serializable exception objects
        for err in detail:
            err.pop("ctx", None)
        return JSONResponse(status_code=422, content={"detail": detail})
    response, record = run_smoothing(request, raw_request=raw)
    try:
        saved = save_run_record(record)
        if saved:
            logger.info("run record saved: %s", saved)
    except Exception:  # persistence must never break the API response
        logger.exception("failed to persist run record")
    return JSONResponse(response)
