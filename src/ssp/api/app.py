"""FastAPI application: routes, error mapping, startup self-check."""
from __future__ import annotations

import contextlib
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from ssp.diagnostics import input_fingerprint, numerical_versions
from ssp.errors import ErrorCategory, PlannerError
from ssp.kernels.normal import validate_noncentral_t

from .schemas import BinomialPlanRequest, ErrorResponse, NormalPlanRequest
from .service import PlanningService

_HTTP_STATUS = {
    ErrorCategory.VALIDATION_ERROR: 422,
    ErrorCategory.EFFECT_TOO_SMALL: 422,
    ErrorCategory.APPROXIMATION_INVALID: 409,
    ErrorCategory.EXACT_CAP_EXCEEDED: 409,
    ErrorCategory.NO_FEASIBLE_SAMPLE: 409,
    ErrorCategory.NONCENTRALITY_ERROR: 500,
    ErrorCategory.NUMERIC_FAILURE: 500,
    ErrorCategory.SIMULATION_ERROR: 500,
    ErrorCategory.PERSISTENCE_ERROR: 500,
}


def create_app(service: PlanningService | None = None) -> FastAPI:
    service = service or PlanningService()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        service.store.initialize()
        # Explicit non-central distribution self-check at boot; a failure here
        # is surfaced, never silently ignored.
        check = validate_noncentral_t()
        worst = max(abs(c["cdf"] - c["p"]) for c in check["size_checks"])
        app.state.noncentral_selfcheck = {"worst_size_error": worst, "detail": check}
        if worst > 1e-9:
            raise RuntimeError(
                f"non-central t startup self-check failed: worst size error {worst:.3e}"
            )
        yield

    app = FastAPI(
        title="Sample Size Planner",
        version="0.1.0",
        description="Fixed-sample size planning for synthetic normal and binomial endpoints.",
        lifespan=lifespan,
    )
    app.state.service = service

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "versions": numerical_versions(),
            "noncentral_selfcheck": getattr(app.state, "noncentral_selfcheck", None),
        }

    @app.post("/api/v1/plans/normal")
    def plan_normal(req: NormalPlanRequest) -> dict[str, Any]:
        try:
            return service.plan_normal(req)
        except PlannerError as exc:
            raise _http_error(exc, req.model_dump())

    @app.post("/api/v1/plans/binomial")
    def plan_binomial(req: BinomialPlanRequest) -> dict[str, Any]:
        try:
            return service.plan_binomial(req)
        except PlannerError as exc:
            raise _http_error(exc, req.model_dump())

    @app.get("/api/v1/runs/{run_id}")
    def get_run(run_id: str) -> dict[str, Any]:
        row = service.store.get(run_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"unknown run_id {run_id}")
        return row

    @app.get("/api/v1/runs")
    def list_runs(limit: int = 50) -> dict[str, Any]:
        return {"runs": service.store.list_runs(limit=limit)}

    @app.exception_handler(Exception)
    async def _unhandled(_request, exc: Exception) -> JSONResponse:  # pragma: no cover
        # Unknown/unexpected states must not look like success.
        body = ErrorResponse(
            error_category=ErrorCategory.NUMERIC_FAILURE.value,
            message=f"unexpected error: {exc}",
            run_id="unassigned",
            input_fingerprint="unassigned",
            details={"type": type(exc).__name__},
            versions=numerical_versions(),
        )
        return JSONResponse(status_code=500, content=body.model_dump())

    return app


def _http_error(exc: PlannerError, request_spec: dict[str, Any]) -> HTTPException:
    status = _HTTP_STATUS.get(exc.category, 500)
    body = {
        "status": "failed",
        "error_category": exc.category.value,
        "message": str(exc),
        "details": exc.details,
        "input_fingerprint": input_fingerprint(request_spec),
        "versions": numerical_versions(),
    }
    return HTTPException(status_code=status, detail=body)


app = create_app()
