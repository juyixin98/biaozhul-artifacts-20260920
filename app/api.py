"""FastAPI service boundary.

Routers stay thin: validation is Pydantic's job, estimation is the service
layer's job, persistence is the store's job. Every error returns a stable
envelope::

    {"error": {"code": ..., "message": ..., "details": ..., "request_id": ...}}

Weak instruments are *not* an HTTP error: the model is still estimated and
returned with ``status="estimated_weak"`` and an ``inconclusive`` verdict.
Identification/rank failures are 422 with explicit failure categories.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Header, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .config import ServiceConfig, load_config
from .contracts import EstimateRequest, EstimateResponse
from .errors import TwoSLSError
from .logging_context import bind_request_id, configure_logging
from .service import run_estimation
from .storage import DecisionStore


def error_envelope(
    code: str, message: str, request_id: str, details: dict | None = None
) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details or {},
            "request_id": request_id,
        }
    }


def register_exception_handlers(app: FastAPI) -> None:
    logger = logging.getLogger("twosls.api")

    @app.exception_handler(TwoSLSError)
    async def domain_error_handler(request: Request, exc: TwoSLSError) -> JSONResponse:
        rid = request.headers.get("X-Request-ID", "-")
        logger.warning("domain error code=%s: %s", exc.code, exc.message)
        return JSONResponse(
            status_code=exc.http_status,
            content=error_envelope(exc.code, exc.message, rid, exc.details),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        rid = request.headers.get("X-Request-ID", "-")
        try:
            errors = exc.errors(include_url=False)
        except TypeError:  # older pydantic
            errors = exc.errors()
        return JSONResponse(
            status_code=422,
            content=error_envelope(
                "invalid_request", "request schema validation failed", rid,
                {"errors": jsonable_encoder(errors)},
            ),
        )


def register_routes(app: FastAPI, config: ServiceConfig,
                    store: DecisionStore) -> None:
    @app.middleware("http")
    async def bind_correlation_id(request: Request, call_next):
        rid = request.headers.get("X-Request-ID") or bind_request_id()
        bind_request_id(rid)
        request.state.request_id = rid
        response = await call_next(request)
        response.headers["X-Request-ID"] = getattr(
            request.state, "request_id", rid
        )
        return response

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/v1/estimate", response_model=EstimateResponse)
    async def estimate(
        request: Request,
        req: EstimateRequest,
        x_request_id: str | None = Header(default=None),
    ) -> EstimateResponse:
        rid = x_request_id or req.request_id or request.state.request_id
        req.request_id = rid
        bind_request_id(rid)
        request.state.request_id = rid
        response = run_estimation(req, config)
        store.save(response)
        return response

    @app.get("/api/v1/runs/{request_id}", response_model=None)
    async def get_run(request_id: str):
        record = store.fetch(request_id)
        if record is None:
            return JSONResponse(
                status_code=404,
                content=error_envelope(
                    "not_found", f"no run with request_id={request_id!r}",
                    request_id,
                ),
            )
        return record

    @app.get("/api/v1/runs")
    async def list_runs(limit: int = 20) -> dict:
        return {"runs": store.recent(max(1, min(limit, 200)))}


def create_app(
    config: ServiceConfig | None = None,
    store: DecisionStore | None = None,
) -> FastAPI:
    config = config or load_config()
    configure_logging(config.log_level)
    store = store or DecisionStore(config.absolute_db_path)

    app = FastAPI(
        title="2SLS estimation service",
        version="0.1.0",
        description=(
            "Two-stage least squares for synthetic linear models with weak-ID "
            "diagnostics. See README for the statistical contract and error codes."
        ),
    )
    register_exception_handlers(app)
    register_routes(app, config, store)
    return app


app = create_app()
