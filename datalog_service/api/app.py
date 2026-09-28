"""FastAPI application factory.

Wires together the request-id/rate-limit middleware, the typed error
handlers and the route table in :mod:`datalog_service.api.routes`.  Every
response carries a ``request_id`` (from the ``X-Request-ID`` header when
supplied, otherwise generated); the same id is written to the SQLite audit
log and can be fetched from ``GET /requests/{request_id}``.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import defaultdict, deque
from typing import Deque, Dict, Tuple

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..config import SETTINGS, Settings
from ..language.errors import CompileError, DatalogError, ParseError, QueryError, StateError
from ..service import DatalogService
from ..storage.evidence_store import EvidenceStore
from .routes import ServiceContext, router

REQUEST_ID_HEADER = "X-Request-ID"


def create_app(store: EvidenceStore | None = None,
               settings: Settings | None = None) -> FastAPI:
    app = FastAPI(
        title="Restricted Datalog Query Service",
        version="1.0.0",
        description="Recursive positive rules with stratified negation.",
    )
    active_settings = settings or SETTINGS
    evidence = store or EvidenceStore(active_settings.db_path)
    app.state.ctx = ServiceContext(
        store=evidence,
        service=DatalogService(evidence),
        settings=active_settings,
    )
    _register_middleware(app, active_settings)
    _register_exception_handlers(app)
    app.include_router(router)
    return app


def _register_middleware(app: FastAPI, settings: Settings) -> None:
    rate_lock = threading.Lock()
    windows: Dict[str, Deque[float]] = defaultdict(deque)

    @app.middleware("http")
    async def request_id_and_rate_limit(request: Request, call_next):
        request_id = request.headers.get(REQUEST_ID_HEADER) or (
            f"req-{uuid.uuid4().hex[:16]}"
        )
        request.state.request_id = request_id
        client = request.client.host if request.client else "unknown"

        limited, retry_after = _check_rate_limit(
            windows, rate_lock, client, settings.rate_limit_per_minute
        )
        if limited:
            response = JSONResponse(
                status_code=429,
                content={
                    "request_id": request_id,
                    "status": "error",
                    "error_code": "RATE_LIMITED",
                    "message": (
                        f"rate limit of {settings.rate_limit_per_minute} "
                        f"requests/minute exceeded; retry after {retry_after}s"
                    ),
                    "details": {"retry_after_seconds": retry_after},
                },
            )
            response.headers["Retry-After"] = str(retry_after)
        else:
            response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response


def _check_rate_limit(
    windows: Dict[str, Deque[float]],
    lock: threading.Lock,
    client: str,
    limit_per_minute: int,
) -> Tuple[bool, int]:
    if limit_per_minute <= 0:
        return False, 0
    now = time.monotonic()
    with lock:
        window = windows[client]
        while window and now - window[0] > 60.0:
            window.popleft()
        if len(window) >= limit_per_minute:
            return True, max(1, int(60 - (now - window[0])))
        window.append(now)
        return False, 0


def _register_exception_handlers(app: FastAPI) -> None:
    statuses = {
        ParseError.code: 400,
        CompileError.code: 422,
        QueryError.code: 400,
        StateError.code: 404,
    }

    @app.exception_handler(DatalogError)
    async def datalog_error_handler(request: Request, exc: DatalogError):
        status_code = statuses.get(exc.code, 400)
        _log_error(request, exc, status_code)
        return JSONResponse(
            status_code=status_code,
            content={
                "request_id": getattr(request.state, "request_id", "unknown"),
                "status": "error",
                "error_code": exc.code,
                "message": exc.message,
                "details": exc.details,
                "failures": exc.details.get("issues", []),
            },
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception):
        return JSONResponse(
            status_code=500,
            content={
                "request_id": getattr(request.state, "request_id", "unknown"),
                "status": "error",
                "error_code": "INTERNAL_ERROR",
                "message": f"{type(exc).__name__}: {exc}",
                "details": {},
            },
        )


def _log_error(request: Request, exc: DatalogError, http_status: int) -> None:
    ctx: ServiceContext | None = getattr(request.app.state, "ctx", None)
    if ctx is None:
        return
    ctx.store.log_request(
        request_id=request.state.request_id,
        endpoint=f"{request.method} {request.url.path}",
        status="error",
        http_status=http_status,
        program_id=request.path_params.get("program_id"),
        goal=getattr(request.state, "goal", None),
        error_code=exc.code,
        error_message=exc.message,
        details=exc.details,
    )
