"""FastAPI service layer for the interval root certification kernel.

Responsibilities: request validation, mapping kernel outcomes to HTTP
statuses, and persisting a replayable JSONL run log. All numerics live in
the kernel; this module only translates contracts.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .errors import (
    CertError,
    DomainEvaluationError,
    InputValidationError,
    StateConflictError,
)
from .kernel import (
    STATUS_COMPLETED,
    STATUS_DOMAIN_ERROR,
    STATUS_RESOURCE_EXHAUSTED,
    CertificationResult,
    KernelConfig,
    certify_roots,
)

LOG_DIR = Path(os.environ.get("INTERVAL_CERT_LOG_DIR", "logs"))
RUN_LOG = LOG_DIR / "runs.jsonl"

app = FastAPI(title="interval-root-certifier", version="0.1.0")


# ---------------------------------------------------------------------------
# Request / response contracts
# ---------------------------------------------------------------------------


class IntervalBounds(BaseModel):
    lo: float
    hi: float


class KernelOptions(BaseModel):
    tol: float = Field(default=1e-12, gt=0)
    max_depth: int = Field(default=50, ge=1, le=200)
    max_intervals: int = Field(default=10000, ge=1, le=1_000_000)
    max_steps: int = Field(default=20000, ge=1, le=2_000_000)
    dps: int = Field(default=50, ge=15, le=300)
    include_trace: bool = True


class CertifyRequest(BaseModel):
    expression: str
    variable: str = "x"
    interval: IntervalBounds
    options: KernelOptions = KernelOptions()


class ErrorBody(BaseModel):
    category: str
    message: str
    details: Optional[dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Error mapping: categories stay distinguishable across the wire
# ---------------------------------------------------------------------------

_CATEGORY_TO_HTTP = {
    "input_error": 400,
    "state_conflict": 409,
    "domain_error": 422,
    "resource_exhausted": 429,
    "compute_failure": 500,
}


@app.exception_handler(CertError)
async def cert_error_handler(_request: Request, exc: CertError) -> JSONResponse:
    status = _CATEGORY_TO_HTTP.get(exc.category, 500)
    return JSONResponse(status_code=status, content={"error": exc.to_dict()})


@app.exception_handler(Exception)
async def unexpected_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    body = {"category": "compute_failure", "message": f"unexpected error: {exc}"}
    return JSONResponse(status_code=500, content={"error": body})


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/certify")
def certify(request: CertifyRequest) -> Any:
    options = request.options
    config = KernelConfig(
        tol=options.tol,
        max_depth=options.max_depth,
        max_intervals=options.max_intervals,
        max_steps=options.max_steps,
        dps=options.dps,
        # The kernel always records the full trace so the persisted run log
        # stays replayable; include_trace only trims the response payload.
        include_trace=True,
    )
    result = certify_roots(
        request.expression,
        request.variable,
        request.interval.lo,
        request.interval.hi,
        config,
    )
    _persist_run(request, result)
    if not options.include_trace:
        result.trace = []

    if result.status == STATUS_COMPLETED:
        return result.to_dict()
    if result.status == STATUS_RESOURCE_EXHAUSTED:
        # A legitimate partial outcome: 200 with a distinguishable status.
        return result.to_dict()
    if result.status == STATUS_DOMAIN_ERROR:
        return JSONResponse(
            status_code=_CATEGORY_TO_HTTP["domain_error"],
            content={**result.to_dict(), "error": result.error},
        )
    return JSONResponse(status_code=500, content=result.to_dict())


def _persist_run(request: CertifyRequest, result: CertificationResult) -> None:
    """Append one JSONL record per run so failures can be replayed offline."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        record = {
            "run_id": result.run_id,
            "request": request.model_dump(),
            "status": result.status,
            "stats": result.stats,
            "error": result.error,
            "certified_count": len(result.certified),
            "undecided_count": len(result.undecided),
            "trace": result.trace,
        }
        with RUN_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except OSError:
        # Logging must never break the computation contract.
        pass
