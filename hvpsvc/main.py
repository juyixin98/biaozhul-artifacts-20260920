"""FastAPI application: Hessian-vector product service.

Endpoints
---------
POST /functions                 register an expression + input layout
POST /functions/{id}/point     set the evaluation point (versioned)
GET  /functions/{id}/value     scalar value at the current point
GET  /functions/{id}/gradient  reverse-mode gradient
POST /functions/{id}/hvp       Hessian-vector product (no dense Hessian)
POST /functions/{id}/verify    independent high-precision verification
GET  /runs/{run_id}            replay a logged run
GET  /healthz                  liveness + catalogue

Error categories map to status codes:
input_error -> 422, state_conflict -> 409, resource_exhausted -> 413/429,
compute_failure -> 422 (domain/overflow), nonsmooth_point -> 409.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .errors import (
    ComputeFailureError,
    ErrorCategory,
    HVPError,
    InputError,
    NonSmoothError,
    ResourceExhaustedError,
    StateConflictError,
)
from .models import (
    CreateFunctionRequest,
    HvpRequest,
    SetPointRequest,
    VerifyRequest,
)
from .ops import op_names
from .runs import RunLogger
from .service import HVPService
from .state import StateStore

_STATUS_BY_CATEGORY = {
    ErrorCategory.INPUT_ERROR: 422,
    ErrorCategory.STATE_CONFLICT: 409,
    ErrorCategory.RESOURCE_EXHAUSTED: 429,
    ErrorCategory.COMPUTE_FAILURE: 422,
    ErrorCategory.NONSMOOTH_POINT: 409,
}


def create_app(service: HVPService | None = None) -> FastAPI:
    app = FastAPI(
        title="HVP Service",
        version="0.1.0",
        description="Restricted differentiable expressions: gradient and "
                    "Hessian-vector products without materializing the Hessian.",
    )
    svc = service or HVPService(store=StateStore(), logger=RunLogger())
    app.state.service = svc

    @app.exception_handler(HVPError)
    async def _on_hvp_error(_: Request, exc: HVPError) -> JSONResponse:
        status = _STATUS_BY_CATEGORY[exc.category]
        return JSONResponse(status_code=status, content={
            "error": exc.to_dict(),
        })

    @app.exception_handler(Exception)
    async def _on_unexpected(_: Request, exc: Exception) -> JSONResponse:
        # Never leak internals; log category is compute_failure bucket.
        return JSONResponse(status_code=500, content={
            "error": {
                "category": "compute_failure",
                "message": f"internal error: {type(exc).__name__}",
                "detail": {},
            }
        })

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"status": "ok", "service": "hvp", "version": app.version,
                "ops": op_names(), "states": len(svc.store)}

    @app.post("/functions", status_code=201)
    async def create_function(req: CreateFunctionRequest) -> dict[str, Any]:
        return svc.create_function(req.model_dump())

    @app.post("/functions/{state_id}/point")
    async def set_point(state_id: str, req: SetPointRequest) -> dict[str, Any]:
        return svc.set_point(state_id, req.point, req.expected_version)

    @app.get("/functions/{state_id}/value")
    async def value(state_id: str) -> dict[str, Any]:
        return svc.value(state_id)

    @app.get("/functions/{state_id}/gradient")
    async def gradient(state_id: str) -> dict[str, Any]:
        return svc.gradient(state_id)

    @app.post("/functions/{state_id}/hvp")
    async def hvp(state_id: str, req: HvpRequest) -> dict[str, Any]:
        return svc.hvp(state_id, req.vector)

    @app.post("/functions/{state_id}/verify")
    async def verify(state_id: str, req: VerifyRequest) -> dict[str, Any]:
        return svc.verify(state_id, req.vector, req.tolerance)

    @app.get("/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        entry = svc.logger.get(run_id)
        if entry is None:
            raise StateConflictError(f"unknown run_id {run_id!r}",
                                     detail={"run_id": run_id})
        return entry

    @app.delete("/functions/{state_id}")
    async def delete_function(state_id: str) -> dict[str, Any]:
        svc.store.delete(state_id)
        return {"deleted": state_id}

    return app


app = create_app()
