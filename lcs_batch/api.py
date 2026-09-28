"""FastAPI surface for the LCS batch retrieval service.

Run locally:
    uvicorn lcs_batch.api:app --port 8000

Request identity: clients may send ``X-Request-ID`` (echoed back and used in
logs/audit rows); otherwise the service generates one per HTTP request. Batch
query bodies may also carry their own ``request_id``.
"""

from __future__ import annotations

import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .config import KERNEL_VERSION, Settings
from .logging_setup import get_logger
from .service import LcsService
from .validation import ErrorCategory, LcsError

_log = get_logger("api")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    service = LcsService(settings)
    app = FastAPI(title="lcs-batch", version=KERNEL_VERSION)
    app.state.service = service

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or f"req-{uuid.uuid4().hex[:16]}"
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        _log.info(
            "http request",
            extra={
                "request_id": request_id,
                "status": response.status_code,
                "detail": f"{request.method} {request.url.path}",
            },
        )
        return response

    @app.exception_handler(LcsError)
    async def lcs_error_handler(request: Request, exc: LcsError) -> JSONResponse:
        status = 404 if exc.category in (
            ErrorCategory.INDEX_NOT_FOUND,
        ) else 400
        _log.info(
            "request rejected",
            extra={
                "request_id": getattr(request.state, "request_id", None),
                "status": status,
                "category": exc.category.value,
            },
        )
        return JSONResponse(
            status_code=status,
            content={
                "request_id": getattr(request.state, "request_id", None),
                "error": exc.to_dict(),
            },
        )

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "kernel_version": KERNEL_VERSION}

    @app.get("/v1/index/info")
    def index_info() -> dict:
        info = service.index_info()
        if info is None:
            raise LcsError(ErrorCategory.INDEX_NOT_FOUND, "no index has been built")
        return info

    @app.post("/v1/index/build")
    def build_index(body: dict, request: Request) -> dict:
        documents = body.get("documents") if isinstance(body, dict) else None
        return service.build_index(
            documents, request_id=request.state.request_id
        )

    @app.post("/v1/query")
    def query(body: dict, request: Request) -> dict:
        if not isinstance(body, dict):
            raise LcsError(ErrorCategory.EMPTY_BATCH, "body must be an object")
        request_id = body.get("request_id") or request.state.request_id
        return service.run_batch(body.get("queries"), request_id=request_id)

    @app.get("/v1/audit/recent")
    def recent_queries(limit: int = 20) -> dict:
        return {"entries": service.recent_queries(limit)}

    return app


app = create_app()
