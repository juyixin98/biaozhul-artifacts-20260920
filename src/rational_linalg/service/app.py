"""FastAPI application: HTTP boundary for the exact rational service.

Endpoints
---------
``GET  /health``                 liveness + arithmetic mode
``POST /api/v1/solve``           solve A x = b exactly
``POST /api/v1/rank``            exact rank + left null space
``GET  /api/v1/runs/{run_id}``   replay a run's events / outcome
``GET  /api/v1/error-codes``     catalogue of distinguishable failure codes

Errors always use the envelope ``{"error": {category, code, message, details}}``
and the four categories map to distinct HTTP statuses (400/409/507/500).
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from ..errors import (
    ComputationFailedError,
    InputError,
    RationalLinAlgError,
    StateConflictError,
)
from ..run_log import RunRegistry
from . import engine
from .schemas import RankRequest, SolveRequest

ERROR_CATALOG: dict[str, dict[str, str]] = {
    "INPUT_ERROR": {
        "http": "400",
        "codes": (
            "INVALID_MATRIX_SHAPE, EMPTY_MATRIX, RAGGED_MATRIX, "
            "INVALID_NUMBER, NON_EXACT_NUMBER, NON_FINITE_NUMBER, "
            "DIMENSION_MISMATCH, MALFORMED_JSON"
        ),
    },
    "STATE_CONFLICT": {
        "http": "409",
        "codes": "RUN_NOT_FOUND",
    },
    "RESOURCE_EXHAUSTED": {
        "http": "507",
        "codes": "DIGIT_BUDGET_EXCEEDED, STEP_BUDGET_EXCEEDED",
    },
    "COMPUTATION_FAILED": {
        "http": "500",
        "codes": "COMPUTATION_FAILED, BAD_PIVOT",
    },
}


def create_app() -> FastAPI:
    app = FastAPI(
        title="Exact Rational Linear Equations & Rank Service",
        version="0.1.0",
        description=(
            "Fraction-free (Bareiss) elimination over the rationals with "
            "digit budgets, parametric solutions and independently verifiable "
            "contradiction evidence."
        ),
    )
    registry = RunRegistry()
    app.state.runs = registry

    @app.exception_handler(RationalLinAlgError)
    async def _service_error(_: Request, exc: RationalLinAlgError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content=exc.to_dict())

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "category": "INPUT_ERROR",
                    "code": "SCHEMA_VALIDATION_FAILED",
                    "message": "request body does not match the schema",
                    "details": {"errors": exc.errors()},
                }
            },
        )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {
            "status": "ok",
            "arithmetic": "exact rational; binary float is never used on the result path",
        }

    @app.get("/api/v1/error-codes")
    async def error_codes() -> dict[str, Any]:
        return {"categories": ERROR_CATALOG}

    def _finish(recorder, response: dict[str, Any]) -> dict[str, Any]:
        recorder.flush()
        response["log_event_count"] = len(recorder.events)
        return response

    def _fail(recorder, exc: RationalLinAlgError) -> None:
        recorder.fail(exc.to_dict()["error"])
        recorder.flush()

    @app.post("/api/v1/solve")
    async def solve(request: SolveRequest) -> dict[str, Any]:
        recorder = registry.create("solve", request.run_id)
        try:
            response = engine.solve_system(request, recorder)
        except RationalLinAlgError as exc:
            _fail(recorder, exc)
            raise
        return _finish(recorder, response)

    @app.post("/api/v1/rank")
    async def rank(request: RankRequest) -> dict[str, Any]:
        recorder = registry.create("rank", request.run_id)
        try:
            response = engine.rank_system(request, recorder)
        except RationalLinAlgError as exc:
            _fail(recorder, exc)
            raise
        return _finish(recorder, response)

    @app.get("/api/v1/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        recorder = registry.get(run_id)
        if recorder is None:
            raise StateConflictError(
                f"unknown run_id {run_id!r}; it belongs to another process or "
                f"was never created",
                code="RUN_NOT_FOUND",
                details={"run_id": run_id},
            )
        return registry.summary(recorder)

    return app


app = create_app()


def run() -> None:  # console-script entry point
    import uvicorn

    uvicorn.run(
        "rational_linalg.service.app:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
    )


if __name__ == "__main__":
    run()
