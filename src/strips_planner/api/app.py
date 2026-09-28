"""HTTP boundary for the STRIPS planning service.

Endpoints (versioned under ``/api/v1``):

* ``POST /plans``          - parse, validate, search and independently verify
* ``POST /plans/execute``  - independently replay a supplied label list
* ``GET  /runs``           - list evidence rows (filter by status/category)
* ``GET  /runs/{run_id}``  - fetch one full evidence row
* ``GET  /healthz``        - liveness

The HTTP layer is deliberately thin: it forwards the decoded JSON body to
:class:`PlanningService` and renders its :class:`ServiceResponse`. Every error
category in :mod:`strips_planner.errors` maps to a distinct status code and is
still persisted with its run id.
"""

from __future__ import annotations

import json
from typing import Any

from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from strips_planner.errors import (
    ErrorCategory,
    ProblemParseError,
    StripsError,
)
from strips_planner.service.planning import PlanningService
from strips_planner.storage.database import EvidenceStore
from strips_planner.storage.run_log import RunLogger


def create_app(*, db_path: str = ":memory:", log_path: str = "logs/runs.jsonl") -> FastAPI:
    store = EvidenceStore(db_path)
    logger = RunLogger(log_path)
    service = PlanningService(store, logger)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.store = store
        app.state.logger = logger
        app.state.service = service
        try:
            yield
        finally:
            # Checkpoint the WAL into the main database file on shutdown.
            store.close()

    app = FastAPI(
        title="STRIPS Offline Planning Service",
        version="1.0.0",
        description=(
            "Offline STRIPS planner with positive/negative preconditions, "
            "add/delete effects, bounded search and independent plan execution."
        ),
        lifespan=lifespan,
    )
    app.state.store = store
    app.state.logger = logger
    app.state.service = service

    @app.exception_handler(StripsError)
    async def strips_error_handler(request: Request, exc: StripsError) -> JSONResponse:
        status = {
            ErrorCategory.NOT_FOUND: 404,
            ErrorCategory.INPUT_INVALID: 400,
            ErrorCategory.INVALID_PROBLEM: 422,
            ErrorCategory.STATE_CONFLICT: 409,
            ErrorCategory.RESOURCE_LIMIT: 200,
        }.get(exc.category, 500)
        return JSONResponse(status_code=status, content={
            "success": False, "result": None, "error": exc.to_dict(),
        })

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "result": None,
                "error": {
                    "category": ErrorCategory.COMPUTATION_FAILED,
                    "code": "UNEXPECTED_ERROR",
                    "message": f"internal error: {exc.__class__.__name__}: {exc}",
                    "details": {},
                },
            },
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/v1/plans")
    async def create_plan(request: Request) -> JSONResponse:
        raw = await request.body()
        payload, syntax_error = _decode_json_body(raw)
        if syntax_error is not None:
            # Undecodable bodies still get a run id and an evidence row so the
            # rejected request can be replayed.
            response = app.state.service.reject_syntax(raw, syntax_error)
            return JSONResponse(status_code=response.http_status, content=response.body)
        response = app.state.service.plan(payload)
        return JSONResponse(status_code=response.http_status, content=response.body)

    @app.post("/api/v1/plans/execute")
    async def execute_plan(request: Request) -> JSONResponse:
        raw = await request.body()
        payload, syntax_error = _decode_json_body(raw)
        if syntax_error is not None:
            response = app.state.service.reject_syntax(raw, syntax_error)
            return JSONResponse(status_code=response.http_status, content=response.body)
        response = app.state.service.execute_plan(payload)
        return JSONResponse(status_code=response.http_status, content=response.body)

    @app.get("/api/v1/runs")
    async def list_runs(
        status: str | None = Query(default=None),
        category: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=500),
    ) -> dict[str, Any]:
        rows = app.state.store.list_runs(status=status, category=category, limit=limit)
        return {"success": True, "result": {"runs": rows, "count": len(rows)}, "error": None}

    @app.get("/api/v1/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        row = app.state.store.get_run(run_id)
        log_lines = app.state.logger.read(run_id)
        row["replay_log"] = log_lines
        return {"success": True, "result": row, "error": None}

    return app


def _decode_json_body(raw: bytes) -> tuple[Any, ProblemParseError | None]:
    """Return ``(parsed, None)`` or ``(None, classified syntax error)``."""
    if not raw:
        return None, ProblemParseError("request body is empty", code="MISSING_FIELD")
    try:
        return json.loads(raw.decode("utf-8")), None
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, ProblemParseError(
            f"request body is not valid JSON: {exc}", code="SYNTAX_ERROR"
        )
