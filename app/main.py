"""Application factory and transport-level middleware.

Run locally with:

    .venv/bin/uvicorn app.main:app --reload

or via :mod:`scripts.run_server`.
"""
from __future__ import annotations

import logging
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .api import router
from .errors import FailureCode
from .settings import Settings, load_settings

logger = logging.getLogger("root_isolation")


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    _configure_logging(settings.service.log_level)

    app = FastAPI(
        title="Rational Polynomial Real-Root Isolation Service",
        version="1.0.0",
        description=(
            "Exact Sturm-sequence isolation of the distinct real roots of a "
            "rational-coefficient polynomial, with multiplicity handling, "
            "endpoint-root care, explicit budgets and independent numeric "
            "evidence."
        ),
    )
    app.state.settings = settings
    app.include_router(router)

    @app.middleware("http")
    async def guard_and_correlate(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
        # The scope dict is shared with the downstream endpoint (unlike a
        # Request.state attached to this middleware's Request instance).
        request.scope["correlation_request_id"] = request_id
        # Bound request bodies before they are parsed; coefficients are the
        # only user data and must fit comfortably inside this envelope.
        if request.method in ("POST", "PUT", "PATCH"):
            declared = request.headers.get("content-length")
            if declared is not None:
                try:
                    if int(declared) > settings.http.max_body_bytes:
                        return _rejected_response(
                            request_id,
                            FailureCode.PAYLOAD_TOO_LARGE,
                            "request body exceeds configured size budget",
                            {"declared_bytes": int(declared),
                             "budget_bytes": settings.http.max_body_bytes},
                            status_code=413,
                        )
                except ValueError:
                    return _rejected_response(
                        request_id, FailureCode.INVALID_COEFFICIENTS,
                        "non-integer content-length header", {}, status_code=400,
                    )

        start = time.monotonic()
        # The access log records shape only — never coefficient values.
        logger.info("request start %s %s request_id=%s",
                    request.method, request.url.path, request_id)
        response = await call_next(request)
        elapsed_ms = (time.monotonic() - start) * 1000
        response.headers["X-Request-ID"] = request_id
        logger.info("request end %s status=%s %.1fms request_id=%s",
                    request.url.path, response.status_code, elapsed_ms,
                    request_id)
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        request_id = request.headers.get("X-Request-ID") or "unassigned"
        # Strip any 'input' echo (may contain user coefficients) from the
        # error detail; keep loc/type/msg which describe the shape problem.
        safe_errors = []
        for err in exc.errors():
            safe_errors.append({
                "loc": [str(part) for part in err.get("loc", [])],
                "type": err.get("type"),
                "msg": err.get("msg"),
            })
        return JSONResponse(
            status_code=422,
            content={
                "request_id": request_id,
                "status": "rejected",
                "degree": None,
                "is_zero_polynomial": False,
                "roots": [],
                "multiplicities": [],
                "evidence": None,
                "diagnostics": {
                    "request_id": request_id,
                    "decision": "rejected",
                    "reason": "request failed schema validation",
                },
                "failure": {
                    "code": FailureCode.INVALID_COEFFICIENTS.value,
                    "message": "request failed schema validation",
                    "state": {"errors": safe_errors},
                },
            },
        )

    return app


def _rejected_response(request_id: str, code: FailureCode, message: str,
                       state: dict, *, status_code: int) -> JSONResponse:
    logger.warning("rejected at transport: %s request_id=%s", code.value,
                   request_id)
    return JSONResponse(
        status_code=status_code,
        headers={"X-Request-ID": request_id},
        content={
            "request_id": request_id,
            "status": "rejected",
            "degree": None,
            "is_zero_polynomial": False,
            "roots": [],
            "multiplicities": [],
            "evidence": None,
            "diagnostics": {
                "request_id": request_id,
                "decision": "rejected",
                "reason": message,
                **{f"state_{k}": v for k, v in state.items()
                   if isinstance(v, (int, float, bool, str))},
            },
            "failure": {"code": code.value, "message": message, "state": state},
        },
    )


app = create_app()
