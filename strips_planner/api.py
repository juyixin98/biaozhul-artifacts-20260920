"""HTTP routes for the planning service."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .errors import (
    COMPUTATION_FAILED,
    INPUT_ERROR,
    REQUEST_MALFORMED,
    RESOURCE_EXHAUSTED,
    RUN_NOT_FOUND,
    STATE_CONFLICT,
    PlannerError,
    ValidationError,
)
from .evidence import EvidenceStore
from .pipeline import run_pipeline

_STATUS_BY_CATEGORY = {
    INPUT_ERROR: 422,
    STATE_CONFLICT: 409,
    RESOURCE_EXHAUSTED: 507,
    COMPUTATION_FAILED: 500,
}


def build_router(store: EvidenceStore) -> APIRouter:
    router = APIRouter()

    @router.get("/health")
    def health() -> dict:
        return {"status": "ok", "service": "strips-planner", "version": "1.0.0"}

    @router.post("/plan")
    async def plan(request: Request) -> JSONResponse:
        raw = await request.body()
        run_id = store.new_run_id()
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else None
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            return _error_response(
                run_id,
                ValidationError(
                    f"request body is not valid JSON: {exc}",
                    code=REQUEST_MALFORMED,
                ),
                store,
                raw,
            )
        if not isinstance(payload, dict):
            return _error_response(
                run_id,
                ValidationError(
                    "request body must be a JSON object",
                    code=REQUEST_MALFORMED,
                ),
                store,
                payload,
            )

        try:
            response = run_pipeline(payload, store, run_id=run_id)
        except PlannerError as exc:
            return JSONResponse(
                status_code=_STATUS_BY_CATEGORY[exc.category],
                content={"run_id": run_id, "error": exc.to_dict()},
            )
        return JSONResponse(status_code=200, content=response)

    @router.get("/runs")
    def list_runs(limit: int = 50) -> dict:
        limit = max(1, min(limit, 500))
        return {"runs": store.list_runs(limit=limit)}

    @router.get("/runs/{run_id}")
    def get_run(run_id: str) -> JSONResponse:
        record = store.get_run(run_id)
        if record is None:
            return JSONResponse(
                status_code=404,
                content={
                    "run_id": run_id,
                    "error": {
                        "category": INPUT_ERROR,
                        "code": RUN_NOT_FOUND,
                        "message": f"no run with id {run_id!r}",
                        "details": [],
                    },
                },
            )
        return JSONResponse(status_code=200, content=record)

    @router.get("/runs/{run_id}/steps")
    def get_steps(run_id: str) -> JSONResponse:
        if store.get_run(run_id) is None:
            return _not_found(run_id)
        return JSONResponse(
            status_code=200,
            content={"run_id": run_id, "steps": store.get_steps(run_id)},
        )

    @router.get("/runs/{run_id}/trace")
    def get_trace(run_id: str) -> JSONResponse:
        if store.get_run(run_id) is None:
            return _not_found(run_id)
        return JSONResponse(
            status_code=200,
            content={"run_id": run_id, "trace": store.get_trace(run_id)},
        )

    @router.post("/runs/{run_id}/replay")
    def replay(run_id: str) -> JSONResponse:
        request = store.replay_request(run_id)
        if request is None:
            return _not_found(run_id)
        new_id = store.new_run_id()
        try:
            response = run_pipeline(request, store, run_id=new_id)
        except PlannerError as exc:
            return JSONResponse(
                status_code=_STATUS_BY_CATEGORY[exc.category],
                content={
                    "run_id": new_id,
                    "replayed_from": run_id,
                    "error": exc.to_dict(),
                },
            )
        response["replayed_from"] = run_id
        return JSONResponse(status_code=200, content=response)

    return router


def _not_found(run_id: str) -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content={
            "run_id": run_id,
            "error": {
                "category": INPUT_ERROR,
                "code": RUN_NOT_FOUND,
                "message": f"no run with id {run_id!r}",
                "details": [],
            },
        },
    )


def _error_response(
    run_id: str, exc: PlannerError, store: EvidenceStore, raw: Any
) -> JSONResponse:
    store.insert_run(
        run_id=run_id,
        request={"_raw": _preview(raw)},
        domain_name=None,
        problem_name=None,
        algorithm=None,
        heuristic=None,
        status=exc.category,
        elapsed_seconds=0.0,
    )
    store.insert_error(
        run_id=run_id,
        category=exc.category,
        code=exc.code,
        message=str(exc),
        details=exc.details,
    )
    return JSONResponse(
        status_code=_STATUS_BY_CATEGORY[exc.category],
        content={"run_id": run_id, "error": exc.to_dict()},
    )


def _preview(raw: Any, limit: int = 2000) -> str:
    if isinstance(raw, bytes):
        return raw[:limit].decode("utf-8", errors="replace")
    return str(raw)[:limit]
