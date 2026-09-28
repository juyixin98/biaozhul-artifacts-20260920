"""FastAPI HTTP surface.

Only transport concerns live here: parse requests, call the service, and map
every :class:`EntityResolutionError` (plus pydantic request errors) to one
stable :class:`ErrorEnvelope`. The four failure categories remain
distinguishable via ``error.category`` and an appropriate HTTP status.

Endpoints
---------
POST /corpus                 load/replace corpus spec
GET  /records                list records with current assignment
POST /links                  add a must/cannot-link (conflict checked)
POST /resolve                run mining; returns clusters + evidence
GET  /clusters               current persisted clusters
POST /clusters/lock          lock a human-confirmed mapping
GET  /affected               change-impact for the latest (or given) run
GET  /health                 liveness
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .config import DEFAULT_CONFIG, AppConfig
from .errors import (
    DuplicateRecordIdError,
    EmptyCorpusError,
    EntityResolutionError,
    ErrorCode,
    InvalidRequestError,
    LockViolationError,
    NoSuchClusterError,
    RecordNotFoundError,
    ResourceExhaustedError,
    VersionConflictError,
)
from .models import (
    ClusterSolution,
    CorpusIn,
    ErrorBody,
    ErrorEnvelope,
    LinkIn,
    LockRequest,
    RecordOut,
)
from .service import EntityResolutionService
from .storage import Storage
from .validation import parse_run_id, parse_threshold

# Category -> default HTTP status when a specific exception does not override.
_CATEGORY_STATUS = {
    "INPUT_ERROR": 400,
    "STATE_CONFLICT": 409,
    "RESOURCE_EXHAUSTED": 507,
    "COMPUTATION_FAILED": 500,
}


def create_app(
    service: EntityResolutionService | None = None,
    db_path: str | None = None,
    log_dir: str | None = None,
    config: AppConfig | None = None,
) -> FastAPI:
    app = FastAPI(
        title="Synthetic Organization Entity Resolution",
        version="1.0.0",
        description="Candidate matching + globally constrained clustering.",
    )

    config = config or DEFAULT_CONFIG
    db_path = db_path or config.db_path
    log_dir = log_dir or config.log_dir
    if service is None:
        service = EntityResolutionService(
            Storage(db_path),
            similarity=config.similarity_config(),
            solver=config.solver_config(),
            log_dir=Path(log_dir),
        )
    app.state.service = service

    def get_service() -> EntityResolutionService:
        return app.state.service

    # ------------------------------------------------------------- errors

    def _envelope(exc: EntityResolutionError, status: int) -> JSONResponse:
        env = ErrorEnvelope(
            error=ErrorBody(
                category=exc.category,
                code=exc.code.value,
                message=exc.message,
                details=exc.details,
                run_id=getattr(exc, "run_id", None),
            )
        )
        return JSONResponse(status_code=status, content=env.model_dump())

    @app.exception_handler(EntityResolutionError)
    async def _domain_handler(request: Request, exc: EntityResolutionError) -> JSONResponse:
        status = _CATEGORY_STATUS.get(exc.category, 500)
        if isinstance(exc, RecordNotFoundError):
            status = 404
        elif isinstance(exc, NoSuchClusterError):
            status = 404
        elif isinstance(exc, DuplicateRecordIdError):
            status = 409
        return _envelope(exc, status)

    @app.exception_handler(ValidationError)
    async def _validation_handler(request: Request, exc: ValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content=ErrorEnvelope(
                error=ErrorBody(
                    category="INPUT_ERROR",
                    code=ErrorCode.INVALID_REQUEST.value,
                    message="request payload failed validation",
                    details={"errors": exc.errors()},
                )
            ).model_dump(),
        )

    # FastAPI wraps body-validation errors in RequestValidationError.
    from fastapi.exceptions import RequestValidationError

    @app.exception_handler(RequestValidationError)
    async def _request_validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content=ErrorEnvelope(
                error=ErrorBody(
                    category="INPUT_ERROR",
                    code=ErrorCode.INVALID_REQUEST.value,
                    message="request payload failed validation",
                    details={"errors": _safe_validation_errors(exc)},
                )
            ).model_dump(),
        )

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        # Never leak internals; map to COMPUTATION_FAILED.
        return JSONResponse(
            status_code=500,
            content=ErrorEnvelope(
                error=ErrorBody(
                    category="COMPUTATION_FAILED",
                    code=ErrorCode.COMPUTATION_FAILED.value,
                    message="unexpected computation failure",
                    details={"type": type(exc).__name__},
                )
            ).model_dump(),
        )

    # ------------------------------------------------------------- routes

    @app.get("/health")
    def health(svc: EntityResolutionService = Depends(get_service)) -> dict:
        return {"status": "ok", "records": svc.storage.count_records()}

    @app.post("/corpus", status_code=201)
    def load_corpus(
        corpus: CorpusIn, svc: EntityResolutionService = Depends(get_service)
    ) -> dict:
        return svc.load_corpus(corpus)

    @app.get("/records", response_model=list[RecordOut])
    def records(svc: EntityResolutionService = Depends(get_service)) -> list[RecordOut]:
        return svc.list_records()

    @app.post("/links", status_code=201)
    def add_link(
        link: LinkIn, svc: EntityResolutionService = Depends(get_service)
    ) -> dict:
        return svc.add_link(link).model_dump()

    @app.post("/resolve", response_model=ClusterSolution)
    def resolve(
        request: Request,
        threshold: str | None = None,
        svc: EntityResolutionService = Depends(get_service),
    ) -> ClusterSolution:
        value = parse_threshold(threshold, svc.sim_cfg.threshold)
        return svc.resolve(value)

    @app.get("/clusters")
    def clusters(svc: EntityResolutionService = Depends(get_service)) -> dict:
        items = svc.storage.list_clusters()
        return {"clusters": items}

    @app.post("/clusters/lock", status_code=201)
    def lock(
        body: LockRequest, svc: EntityResolutionService = Depends(get_service)
    ) -> dict:
        return svc.lock_cluster(body.cluster_id, body.members, body.expected_version)

    @app.get("/affected")
    def affected(
        run_id: str | None = None,
        svc: EntityResolutionService = Depends(get_service),
    ) -> dict:
        return svc.affected_entities(parse_run_id(run_id)).model_dump()

    return app


def _safe_validation_errors(exc: Any) -> list[dict]:
    """Make pydantic error objects JSON-serializable and free of input echo."""
    cleaned: list[dict] = []
    for err in exc.errors():
        ctx = err.get("ctx") or {}
        cleaned.append(
            {
                "location": list(err.get("loc", [])),
                "type": err.get("type"),
                "message": err.get("msg"),
                "context_keys": sorted(ctx.keys()),
            }
        )
    return cleaned


# Default ASGI application (uses ER_DB_PATH / ER_LOG_DIR env vars).
app = create_app()
