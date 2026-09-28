"""FastAPI application factory: routes, correlation-id middleware, errors."""
from __future__ import annotations

import logging
import time

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .. import __version__
from ..config import settings
from ..services import comparison, synthetic
from ..services.diagnostics import (
    configure_logging,
    log_event,
    new_request_id,
    request_id_var,
    summarize_input,
)
from ..services.inputio import InputValidationError, parse_values, validate_block_size
from .schemas import CompareRequest, ScenarioRequest

_REQUEST_ID_HEADER = "x-request-id"


def _sanitize_validation_errors(errors: list[dict]) -> list[dict]:
    """Make pydantic error entries JSON-safe and free of raw input values."""
    safe: list[dict] = []
    for err in errors:
        entry = {
            "loc": [str(part) for part in err.get("loc", [])],
            "msg": str(err.get("msg", "")),
            "type": str(err.get("type", "")),
        }
        # ctx often embeds the original exception object; stringify it.
        ctx = err.get("ctx")
        if ctx:
            entry["ctx"] = {
                key: str(value) if not isinstance(value, (str, int, float, bool, type(None))) else value
                for key, value in ctx.items()
            }
        # 'input' may carry the caller's (sensitive) raw payload: omit it.
        safe.append(entry)
    return safe


def _error_envelope(request_id: str, code: str, message: str, status: int, **extra) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "ok": False,
            "error": {"code": code, "message": message, **extra},
            "request_id": request_id,
        },
        headers={_REQUEST_ID_HEADER: request_id},
    )


def _selected_orderings(orderings: list[str] | None) -> list[str]:
    return orderings if orderings else ["original", "reversed", "abs_ascending"]


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""
    configure_logging()
    application = FastAPI(
        title="Summation Comparison API",
        version=__version__,
        description=(
            "Compares naive, pairwise and Kahan-compensated summation (including "
            "chunked variants that carry compensation state) against mpmath and "
            "extended-precision reference sums."
        ),
    )

    @application.middleware("http")
    async def correlation_and_access_log(request: Request, call_next):
        request_id = request.headers.get(_REQUEST_ID_HEADER) or new_request_id()
        token = request_id_var.set(request_id)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            log_event(
                "http_request",
                method=request.method,
                path=request.url.path,
                elapsed_ms=round(elapsed_ms, 3),
            )
            request_id_var.reset(token)
        response.headers[_REQUEST_ID_HEADER] = request_id
        return response

    @application.exception_handler(InputValidationError)
    async def input_validation_handler(request: Request, exc: InputValidationError) -> JSONResponse:
        log_event("input_rejected", level=logging.WARNING, **exc.to_plain())
        return _error_envelope(
            request_id_var.get(), exc.code, exc.message, 422, index=exc.index
        )

    @application.exception_handler(RequestValidationError)
    async def request_validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        safe_errors = _sanitize_validation_errors(exc.errors())
        log_event("schema_rejected", level=logging.WARNING, detail=safe_errors)
        return _error_envelope(
            request_id_var.get(), "schema_validation_error",
            "request body failed schema validation", 422, detail=safe_errors,
        )

    @application.get("/health")
    async def health() -> dict:
        return {
            "ok": True,
            "service": settings.service_name,
            "version": __version__,
            "request_id": request_id_var.get(),
        }

    @application.get("/api/v1/info")
    async def info() -> dict:
        return {
            "service": settings.service_name,
            "version": __version__,
            "methods": {
                "monolithic": list(comparison.MONOLITHIC_METHODS),
                "chunked": list(comparison.CHUNKED_METHODS),
            },
            "orderings": list(comparison.ORDERINGS),
            "scenarios": sorted(synthetic.SCENARIOS),
            "config": {
                "max_input_length": settings.max_input_length,
                "default_block_size": settings.default_block_size,
                "reference_precision_digits": settings.reference_precision,
                "reference_max_length": settings.reference_max_length,
                "error_tolerance_factor": settings.error_tolerance_factor,
            },
            "request_id": request_id_var.get(),
        }

    @application.post("/api/v1/compare")
    async def compare(body: CompareRequest) -> dict:
        values = parse_values(body.values)
        block_size = validate_block_size(body.block_size)
        orderings = _selected_orderings(body.orderings)
        log_event(
            "compare_request", **summarize_input(values),
            block_size=block_size, orderings=orderings,
        )
        result = comparison.run_comparison(values, block_size, orderings, body.shuffle_seed)
        result["ok"] = True
        result["request_id"] = request_id_var.get()
        return result

    @application.post("/api/v1/scenario")
    async def scenario(body: ScenarioRequest) -> dict:
        values = synthetic.build_scenario(body.scenario, body.n)
        block_size = validate_block_size(body.block_size)
        orderings = _selected_orderings(body.orderings)
        log_event(
            "scenario_request", scenario=body.scenario, n=body.n,
            block_size=block_size, orderings=orderings,
        )
        result = comparison.run_comparison(values, block_size, orderings, body.shuffle_seed)
        result["ok"] = True
        result["request_id"] = request_id_var.get()
        result["scenario"] = {"name": body.scenario, "n": body.n}
        return result

    return application


app = create_app()
