"""FastAPI application factory and HTTP routes.

Failure policy: invalid input is a 400 with a typed error envelope; an
unknown run is a 404; genuine internal faults are a 500 carrying the
``INTERNAL_ERROR`` category. No exception path is collapsed into a 200.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app import __version__
from app.config import Settings, runtime_versions
from app.planner.enumerate import ReferenceAnswer
from app.rules.errors import FailureCategory, PlanningError, ValidationFailure
from app.storage import EvidenceStore, connect, init_schema
from .schemas import (
    ReferenceRequest,
    ReferenceSummary,
    ReplayRequest,
    ReplayResponse,
    RunRef,
    SolveRequest,
    SolveResponse,
)
from .service import PlanningService

logger = logging.getLogger("temporal-planner.api")


def _error_envelope(category: str, message: str, **context: Any) -> dict[str, Any]:
    return {"error": {"category": category, "message": message, "context": context}}


def create_app(settings: Settings | None = None, *, store: EvidenceStore | None = None) -> FastAPI:
    settings = settings or Settings()
    app = FastAPI(
        title="Temporal Planner (finite action set)",
        version=__version__,
        description="Start conditions, duration invariants and end effects on an integer time grid.",
    )
    app.state.settings = settings

    logging.basicConfig(level=getattr(logging, settings.log_level, logging.INFO))

    if store is None:
        conn = connect(settings.db_path, foreign_keys=settings.sqlite_foreign_keys)
        init_schema(conn)
        store = EvidenceStore(conn)
    app.state.store = store
    app.state.service = PlanningService(store, engine_version=__version__)

    # ---- exception handlers (no failure ever surfaces as a 200) --------

    @app.exception_handler(RequestValidationError)
    async def _on_request_validation(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content=_error_envelope(
                FailureCategory.INVALID_INPUT,
                "request validation failed",
                errors=jsonable_encoder(exc.errors()),
            ),
        )

    def _wrap(exc: PlanningError) -> dict[str, Any]:
        body = exc.to_dict()
        return {"error": {"category": body["category"], "message": body["message"], "context": body["context"]}}

    @app.exception_handler(ValidationFailure)
    async def _on_validation_failure(_request: Request, exc: ValidationFailure) -> JSONResponse:
        return JSONResponse(status_code=400, content=_wrap(exc))

    @app.exception_handler(PlanningError)
    async def _on_planning_error(_request: Request, exc: PlanningError) -> JSONResponse:
        return JSONResponse(status_code=422, content=_wrap(exc))

    @app.exception_handler(Exception)
    async def _on_internal_error(request: Request, exc: Exception) -> JSONResponse:
        incident = str(uuid.uuid4())
        logger.exception("unhandled error incident=%s path=%s", incident, request.url.path)
        return JSONResponse(
            status_code=500,
            content=_error_envelope(
                FailureCategory.INTERNAL_ERROR,
                "internal error",
                incident_id=incident,
                type=type(exc).__name__,
            ),
        )

    # ---- routes --------------------------------------------------------

    @app.get("/health")
    async def health() -> dict[str, object]:
        return {"status": "ok", "version": __version__}

    @app.get("/version")
    async def version() -> dict[str, str]:
        return runtime_versions()

    @app.post("/api/solve", response_model=SolveResponse)
    def solve(request: SolveRequest) -> SolveResponse:
        outcome = app.state.service.solve(
            request.problem,
            budget_nodes=request.options.budget_nodes,
            max_steps=request.options.max_steps,
            max_occurrences_per_action=request.options.max_occurrences_per_action,
            cross_check=request.options.cross_check_reference,
            reference_max_steps=request.options.reference_max_steps,
            reference_max_occurrences=request.options.reference_max_occurrences,
        )
        reference: ReferenceSummary | None = None
        if outcome.reference is not None:
            ref: ReferenceAnswer = outcome.reference
            reference = ReferenceSummary(
                found=ref.found,
                optimal_makespan=ref.optimal_makespan,
                schedules_evaluated=ref.schedules_evaluated,
            )
        return SolveResponse(
            run=RunRef(
                run_id=outcome.run_id,
                input_fingerprint=outcome.fingerprint,
                engine_version=outcome.engine_version,
            ),
            search=outcome.search,
            replay=outcome.replay_result,
            reference=reference,
            cross_check_agrees=outcome.cross_check_agrees,
            cross_check_detail=outcome.cross_check_detail,
        )

    @app.post("/api/replay", response_model=ReplayResponse)
    def replay_endpoint(request: ReplayRequest) -> ReplayResponse:
        run_id, fp, engine_version, result = app.state.service.replay_only(request.problem, request.plan)
        return ReplayResponse(
            run=RunRef(run_id=run_id, input_fingerprint=fp, engine_version=engine_version),
            replay=result,
        )

    @app.post("/api/reference", response_model=ReferenceSummary)
    def reference_endpoint(request: ReferenceRequest) -> ReferenceSummary:
        answer = app.state.service.reference_only(
            request.problem,
            max_steps=request.max_steps,
            max_occurrences_per_action=request.max_occurrences_per_action,
        )
        return ReferenceSummary(
            found=answer.found,
            optimal_makespan=answer.optimal_makespan,
            schedules_evaluated=answer.schedules_evaluated,
        )

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> dict[str, Any]:
        record = app.state.store.get_run(run_id)
        if record is None:
            return JSONResponse(
                status_code=404,
                content=_error_envelope("NOT_FOUND", f"unknown run_id {run_id!r}", run_id=run_id),
            )
        return record

    @app.get("/api/runs/{run_id}/events")
    def get_events(run_id: str) -> dict[str, Any]:
        if app.state.store.get_run(run_id) is None:
            return JSONResponse(
                status_code=404,
                content=_error_envelope("NOT_FOUND", f"unknown run_id {run_id!r}", run_id=run_id),
            )
        return {"run_id": run_id, "events": app.state.store.get_events(run_id)}

    @app.get("/api/runs/{run_id}/violations")
    def get_violations(run_id: str) -> dict[str, Any]:
        if app.state.store.get_run(run_id) is None:
            return JSONResponse(
                status_code=404,
                content=_error_envelope("NOT_FOUND", f"unknown run_id {run_id!r}", run_id=run_id),
            )
        return {"run_id": run_id, "violations": app.state.store.get_violations(run_id)}

    @app.get("/api/runs")
    def list_runs(limit: int = 50, kind: str | None = None) -> dict[str, Any]:
        limit = max(1, min(limit, 200))
        return {"runs": app.state.store.list_runs(limit=limit, kind=kind)}

    return app


def run() -> None:  # pragma: no cover - manual entry point
    import uvicorn

    settings = Settings()
    uvicorn.run(create_app(settings), host="127.0.0.1", port=8000)
