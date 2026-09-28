"""FastAPI service layer.

Endpoints:
* GET  /healthz              -> liveness + config fingerprint
* POST /api/v1/ipw/estimate  -> estimate ATE/ATT/ATU with full evidence record
* GET  /api/v1/runs/{id}     -> fetch a persisted run (when persistence enabled)

Failure responses always carry the stable error ``code`` from
:mod:`ipwate.errors` so clients can branch on failure category.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .config import AppConfig, load_config
from .errors import IPWError, PositivityError
from .pipeline import run_ipw
from .storage import load_run

logging.basicConfig(level=logging.INFO, format="%(message)s")


class EstimateRequest(BaseModel):
    x: list[list[float]] = Field(..., description="Covariate matrix, n x p")
    a: list[int] = Field(..., description="Binary treatment indicator {0,1}")
    y: list[float] = Field(..., description="Observed outcome")
    estimand: str | None = Field(None, pattern="^(ate|att|atu)$")
    weight_type: str | None = Field(None, pattern="^(stabilized|ht)$")
    n_splits: int | None = Field(None, ge=2, le=20)
    seed: int | None = None
    clipping_enabled: bool | None = None
    request_id: str | None = Field(None, max_length=80)
    persist: bool = False


def _overrides_from(req: EstimateRequest) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    if req.estimand is not None:
        overrides["estimand"] = req.estimand
    if req.weight_type is not None:
        overrides["weight_type"] = req.weight_type
    if req.n_splits is not None:
        overrides["crossfit.n_splits"] = req.n_splits
    if req.seed is not None:
        overrides["crossfit.seed"] = req.seed
    if req.clipping_enabled is not None:
        overrides["weights.clipping.enabled"] = req.clipping_enabled
    return overrides


def create_app(config: AppConfig | None = None) -> FastAPI:
    config = config or load_config()
    app = FastAPI(
        title="IPW-ATE evidence service",
        version="0.1.0",
        description=(
            "Cross-fitted inverse-probability-weighted treatment effect estimation "
            "with overlap diagnostics. Synthetic/local data only. Outputs are "
            "conditional on unconfoundedness and positivity; they are not causal proof."
        ),
    )
    app.state.config = config

    @app.exception_handler(IPWError)
    async def ipw_error_handler(request: Request, exc: IPWError) -> JSONResponse:
        rid = getattr(request.state, "request_id", None)
        return JSONResponse(
            status_code=exc.http_status,
            content={
                "ok": False,
                "request_id": rid,
                "error": exc.to_dict(),
            },
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        rid = getattr(request.state, "request_id", None)
        return JSONResponse(
            status_code=422,
            content={
                "ok": False,
                "request_id": rid,
                "error": {
                    "code": "validation_error",
                    "message": "Request failed schema validation",
                    "details": {"errors": exc.errors()},
                },
            },
        )

    @app.middleware("http")
    async def add_request_id(request: Request, call_next):
        rid = request.headers.get("x-request-id") or f"req-{uuid.uuid4()}"
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["x-request-id"] = rid
        return response

    @app.get("/healthz")
    async def healthz(request: Request) -> dict[str, Any]:
        return {
            "status": "ok",
            "request_id": request.state.request_id,
            "config_path": config.source_path,
            "estimand_default": config.estimand,
            "weight_type_default": config.weight_type,
            "crossfit_n_splits": config.crossfit.n_splits,
            "clipping_default": config.weights.clipping.enabled,
        }

    @app.post("/api/v1/ipw/estimate")
    async def estimate(request: Request, req: EstimateRequest) -> dict[str, Any]:
        result = run_ipw(
            req.x,
            req.a,
            req.y,
            config=config,
            overrides=_overrides_from(req),
            request_id=req.request_id or request.state.request_id,
            persist=req.persist,
        )
        return {"ok": True, "request_id": result.request_id, "result": result.to_dict()}

    @app.get("/api/v1/runs/{request_id}")
    async def get_run(request: Request, request_id: str) -> dict[str, Any]:
        record = load_run(config.storage.db_path, request_id)
        if record is None:
            return JSONResponse(
                status_code=404,
                content={
                    "ok": False,
                    "request_id": request.state.request_id,
                    "error": {"code": "not_found", "message": "unknown request_id"},
                },
            )
        return {"ok": True, "request_id": request_id, "result": record}

    return app


app = create_app()
