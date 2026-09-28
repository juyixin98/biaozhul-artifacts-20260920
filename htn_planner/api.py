"""FastAPI query interface.

Every response carries the ``request_id`` (client-supplied via the
``X-Request-ID`` header, or server-generated) and structured logs carry the
same id, so API output and log lines can be correlated.  Definitive failures
(``failures``) and inconclusive budget cuts (``uncertain``) are always
separate fields; verification results are reported separately from the
planner's own verdict.
"""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .config import API_V1, Settings
from .core.engine import ENGINE_VERSION
from .logging_setup import configure_logging, request_id_var
from .service import PlanningService, RequestError
from .store import EvidenceStore, StoreError

logger = logging.getLogger("htn_planner.api")


class PlanRequestBody(BaseModel):
    domain: str = Field(..., min_length=1, description="Domain rule-language text")
    problem: str = Field(..., min_length=1, description="Problem rule-language text")
    domain_version: str = Field(
        default="local-unversioned",
        description="Client-assigned version label for the domain text",
    )
    bounds: dict[str, int] | None = Field(
        default=None, description="Optional per-request bound overrides"
    )


class ErrorBody(BaseModel):
    request_id: str
    error: str
    detail: str
    engine_version: str


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Any:
        configure_logging(settings.log_level)

        store = EvidenceStore(settings.db_path)
        app.state.store = store
        app.state.service = PlanningService(store, settings.bounds)
        app.state.settings = settings
        logger.info(
            "service starting host=%s port=%s db=%s engine=%s",
            settings.host,
            settings.port,
            settings.db_path,
            ENGINE_VERSION,
        )
        try:
            yield
        finally:
            store.close()

    app = FastAPI(
        title="Bounded HTN Planner",
        version=settings.service_version,
        description=(
            "Hierarchical Task Network planning with method selection, ordered"
            " and partially-ordered subtasks, bounded recursion and evidence."
        ),
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def bind_request_id(request: Request, call_next: Any) -> Any:
        incoming = request.headers.get("x-request-id")
        request_id = incoming or f"req-{uuid.uuid4().hex[:12]}"
        request.state.request_id = request_id
        token = request_id_var.set(request_id)
        start = time.perf_counter()
        logger.info("%s %s handler=start", request.method, request.url.path)
        try:
            response = await call_next(request)
        except Exception:
            logger.exception("%s %s handler=error", request.method, request.url.path)
            raise
        finally:
            request_id_var.reset(token)
        elapsed = round((time.perf_counter() - start) * 1000, 2)
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "%s %s handler=done status=%s elapsed_ms=%s",
            request.method,
            request.url.path,
            response.status_code,
            elapsed,
        )
        return response

    @app.exception_handler(RequestError)
    async def request_error_handler(request: Request, exc: RequestError) -> JSONResponse:
        body = ErrorBody(
            request_id=getattr(request.state, "request_id", "-"),
            error="invalid_request",
            detail=exc.message,
            engine_version=ENGINE_VERSION,
        )
        return JSONResponse(status_code=exc.status_code, content=body.model_dump())

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "engine_version": ENGINE_VERSION}

    @app.post(f"{API_V1}/plan")
    def plan(
        body: PlanRequestBody,
        request: Request,
        x_request_id: str | None = Header(default=None),
    ) -> dict[str, Any]:
        request_id = getattr(request.state, "request_id", None) or x_request_id
        if request_id is None:  # pragma: no cover - middleware always sets it
            raise HTTPException(status_code=500, detail="missing request id")
        service: PlanningService = request.app.state.service
        try:
            response = service.plan(
                domain_text=body.domain,
                problem_text=body.problem,
                request_id=request_id,
                domain_version=body.domain_version,
                bounds_overrides=body.bounds,
            )
        except StoreError as exc:
            raise RequestError(str(exc), status_code=409) from exc
        payload = response.to_dict()
        payload["request_id"] = request_id
        return payload

    @app.get(f"{API_V1}/runs")
    def list_runs(
        request: Request,
        status_filter: str | None = Query(default=None, alias="status"),
        limit: int = Query(default=50, ge=1, le=500),
    ) -> dict[str, Any]:
        store: EvidenceStore = request.app.state.store
        rows = store.list_runs(limit=limit, status=status_filter)
        return {
            "request_id": request.state.request_id,
            "count": len(rows),
            "runs": rows,
        }

    @app.get(f"{API_V1}/runs/{{request_id}}")
    def get_run(request_id: str, request: Request) -> dict[str, Any]:
        store: EvidenceStore = request.app.state.store
        row = store.get_run(request_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"unknown request {request_id}")
        return {"request_id": request.state.request_id, "run": row}

    @app.get(f"{API_V1}/runs/{{request_id}}/verification")
    def get_verification(request_id: str, request: Request) -> dict[str, Any]:
        store: EvidenceStore = request.app.state.store
        row = store.get_verification(request_id)
        if row is None:
            raise HTTPException(
                status_code=404, detail=f"no verification for request {request_id}"
            )
        return {"request_id": request.state.request_id, "verification": row}

    return app


app = create_app()
