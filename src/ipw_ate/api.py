"""FastAPI service exposing the IPW-ATE kernel.

Run:  uvicorn ipw_ate.api:app --reload
The service validates input, runs cross-fitted IPW, persists an aggregate
record keyed by request id, and returns the estimate plus an overlap verdict.
Failures are mapped to stable HTTP status + machine-readable ``error.code``.
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .contract import Estimand, IPWConfig
from .errors import (
    DataValidationError,
    EstimationError,
    InsufficientDataError,
    IPWError,
    ModelSeparationError,
    OverlapViolationError,
    PropensityScoreError,
)
from .pipeline import run_ipw
from .storage import RunStore

logger = logging.getLogger("ipw_ate.api")

app = FastAPI(
    title="IPW-ATE with overlap diagnostics",
    version="1.0.0",
    description="Cross-fitted stable IPW average treatment effect; never a "
    "substitute for the unconfoundedness/positivity assumptions.",
)

# Store path is overridable (IPW_RUNS_DB) so tests get an isolated database;
# only aggregate records are persisted.
_store: RunStore | None = None


def get_store() -> RunStore:
    global _store
    if _store is None:
        _store = RunStore(os.environ.get("IPW_RUNS_DB", "data/runs.db"))
    return _store


def reset_store(path: str | None = None) -> None:
    """Test/admin hook to point the store at another file."""
    global _store
    _store = RunStore(path) if path else None

#: Map classified kernel errors -> HTTP status.
_STATUS: dict[type[IPWError], int] = {
    DataValidationError: 422,
    InsufficientDataError: 422,
    PropensityScoreError: 422,
    ModelSeparationError: 422,
    OverlapViolationError: 409,
    EstimationError: 422,
}


class AnalysisRequest(BaseModel):
    treatment: list[int] = Field(..., description="binary 0/1 assignment")
    outcome: list[float] = Field(..., description="observed outcome")
    covariates: list[list[float]] = Field(..., description="n x p matrix")
    estimand: Estimand = Estimand.ATE
    n_splits: int = Field(5, ge=2, le=50)
    request_id: str | None = Field(None, description="client correlation id")


def _diag_payload(d: Any) -> dict[str, Any]:
    return {
        "decision": d.decision.value,
        "reasons": list(d.reasons),
        "message": d.message,
        "n": d.n,
        "n_treated": d.n_treated,
        "n_control": d.n_control,
        "n_splits": d.n_splits,
        "estimand": d.estimand,
        "trim_version": d.trim_version,
        "score_min": d.score_min,
        "score_max": d.score_max,
        "n_extreme_scores": d.n_extreme_scores,
        "n_boundary_scores": d.n_boundary_scores,
        "max_weight": d.max_weight,
        "max_balance_z": d.max_balance_z,
        "balance_basis": d.balance_basis,
        "ess_treated": d.ess_treated,
        "ess_control": d.ess_control,
        "single_arm_cells": d.single_arm_cells,
        "assumptions": list(d.assumptions),
        "request_id": d.request_id,
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/analyze")
def analyze(req: AnalysisRequest) -> JSONResponse:
    import numpy as np

    request_id = req.request_id or f"req_{uuid.uuid4().hex[:12]}"
    config = IPWConfig(estimand=req.estimand, n_splits=req.n_splits)
    t = np.asarray(req.treatment, dtype=float)
    y = np.asarray(req.outcome, dtype=float)
    x = np.asarray(req.covariates, dtype=float)

    try:
        result = run_ipw(t, y, x, config=config, request_id=request_id)
    except OverlapViolationError as exc:
        d = exc.diagnostic
        if d is not None:
            get_store().record_diagnostic(d, OverlapViolationError.code)
        body = {
            "error": {
                "code": OverlapViolationError.code,
                "message": str(exc),
                "request_id": request_id,
                "diagnostic": _diag_payload(d) if d is not None else None,
            }
        }
        return JSONResponse(status_code=409, content=body)
    except IPWError as exc:
        status = _STATUS.get(type(exc), 422)
        # Log aggregates only.
        logger.warning("request %s failed: %s", request_id, exc.code)
        body = {
            "error": {
                "code": exc.code,
                "message": str(exc),
                "request_id": request_id,
            }
        }
        return JSONResponse(status_code=status, content=body)

    get_store().record_result(result)
    d = result.diagnostic
    return JSONResponse(
        status_code=200,
        content={
            "request_id": request_id,
            "estimand": result.estimand,
            "estimate": result.estimate,
            "std_error": result.std_error,
            "ci": {
                "level": result.ci_level,
                "lower": result.ci_lower,
                "upper": result.ci_upper,
            },
            "diagnostic": _diag_payload(d),
            "caveat": (
                "Estimate is causal only under unconfoundedness, positivity "
                "and SUTVA. A diagnostic verdict is an overlap/weight check, "
                "not proof of a causal effect."
            ),
        },
    )


@app.get("/runs/{request_id}")
def get_run(request_id: str) -> JSONResponse:
    row = get_store().get(request_id)
    if row is None:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": "not_found", "request_id": request_id}},
        )
    return JSONResponse(status_code=200, content=row)
