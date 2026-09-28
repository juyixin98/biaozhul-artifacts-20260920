"""FastAPI HTTP adapter.

Thin transport layer over :func:`root_isolator.service.application.isolate_from_payload`.
It adds request ids, maps :class:`IsolationError` to the stable error envelope
and decides HTTP status from the error category.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from root_isolator import __version__
from root_isolator.errors import (
    HTTP_STATUS_BY_CATEGORY,
    ErrorCategory,
    IsolationError,
)
from root_isolator.service.application import isolate_from_payload
from root_isolator.service.diagnostics import Diagnostics, configure_logging
from root_isolator.service.schemas import HealthResponse, IsolationRequest
from root_isolator.service.serialization import serialize_error, serialize_verdict
from config.settings import ServiceConfig, load_settings


def create_app(config: ServiceConfig | None = None) -> FastAPI:
    config = config or load_settings()
    logger = configure_logging(config.log)
    app = FastAPI(
        title="Sturm Real-Root Isolation Service",
        version=__version__,
        description=(
            "Isolates the real roots of an exact rational-coefficient "
            "polynomial into pairwise disjoint rational intervals using Sturm "
            "sequences, with independent Descartes, mpmath and float64 evidence."
        ),
    )

    @app.exception_handler(IsolationError)
    async def isolation_error_handler(_: Request, exc: IsolationError) -> JSONResponse:
        request_id = exc.request_id or str(uuid.uuid4())
        diagnostics = Diagnostics(logger, config.log, request_id)
        diagnostics.rejected_request(
            exc.category.value,
            exc.message,
            **{k: v for k, v in exc.state.items() if k != "coefficients"},
        )
        status_code = HTTP_STATUS_BY_CATEGORY.get(exc.category, 500)
        return JSONResponse(
            status_code=status_code,
            content=serialize_error(
                request_id=request_id,
                category=exc.category.value,
                message=exc.message,
                state=exc.state,
            ),
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(_: Request, exc: Exception) -> JSONResponse:
        request_id = str(uuid.uuid4())
        logger.exception(
            "unhandled error: %s",
            exc,
            extra={"request_id": request_id, "event": "internal_error"},
        )
        return JSONResponse(
            status_code=500,
            content=serialize_error(
                request_id=request_id,
                category=ErrorCategory.INTERNAL_ERROR.value,
                message="an unexpected internal error occurred",
                state={"error_type": type(exc).__name__},
            ),
        )

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(
            status="ok",
            service="sturm-root-isolator",
            version=__version__,
        )

    @app.post("/api/v1/isolate")
    async def isolate(request: Request) -> JSONResponse:
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        diagnostics = Diagnostics(logger, config.log, request_id)
        try:
            raw = await request.json()
        except Exception as exc:
            raise IsolationError(
                ErrorCategory.MALFORMED_COEFFICIENTS,
                f"request body is not valid JSON: {exc}",
                state={},
                request_id=request_id,
            )
        if not isinstance(raw, dict):
            raise IsolationError(
                ErrorCategory.MALFORMED_COEFFICIENTS,
                "request body must be a JSON object",
                state={"type": type(raw).__name__},
                request_id=request_id,
            )

        # Validate shape with pydantic; exactness is enforced in the input layer.
        try:
            model = IsolationRequest.model_validate(raw)
        except Exception as exc:
            raise IsolationError(
                ErrorCategory.MALFORMED_COEFFICIENTS,
                f"request does not match the expected schema: {exc}",
                state={},
                request_id=request_id,
            ) from exc
        payload: dict[str, Any] = {"order": model.order}
        if model.coefficients is not None:
            payload["coefficients"] = model.coefficients
        if model.sparse is not None:
            payload["sparse"] = model.sparse

        outcome = isolate_from_payload(
            payload,
            budget=config.budget,
            numeric=config.numeric,
            diagnostics=diagnostics,
        )
        if outcome.verdict.status == "rejected":
            # Exact witnesses disagree: a defect/arithmetic fault, not a normal
            # answer. Surface as a 409 evidence mismatch with the full reasons.
            raise IsolationError(
                ErrorCategory.EVIDENCE_MISMATCH,
                "exact root-isolation evidence is internally inconsistent; "
                "result withheld",
                state={"reasons": list(outcome.verdict.reasons)},
                request_id=request_id,
            )
        body = serialize_verdict(
            outcome.verdict, request_id=request_id, meta=outcome.meta
        )
        status_code = 200
        return JSONResponse(status_code=status_code, content=body)

    return app


app = create_app()
