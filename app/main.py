"""FastAPI application: dataset ingest and audited association rule queries."""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable, Dict

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .config import get_settings
from .diagnostics import new_request_id
from .repository import Database
from .schemas import (
    ErrorResponse,
    IngestRequest,
    IngestResponse,
    RuleRequest,
    RuleResponse,
    rule_to_out,
)
from .service import RuleAuditService
from .validation import QueryValidationError

logger = logging.getLogger("rule_audit.api")


class ServiceHolder:
    def __init__(self) -> None:
        self.db: Database | None = None
        self.service: RuleAuditService | None = None


holder = ServiceHolder()


@asynccontextmanager
async def lifespan(app: FastAPI) -> Any:
    settings = get_settings()
    holder.db = Database(settings.db_path)
    holder.service = RuleAuditService(holder.db, settings)
    logger.info("rule-audit API started with database %s", settings.db_path)
    try:
        yield
    finally:
        holder.db.close()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Association Rule Lift Audit API",
        version="1.0.0",
        description=(
            "Generates association rules with confidence, lift and leverage "
            "from pre-mined frequent itemsets, with explicit undefined/zero-"
            "denominator semantics and per-rule audit verdicts."
        ),
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def add_request_id(
        request: Request, call_next: Callable[[Request], Awaitable[Any]]
    ) -> Any:
        incoming = request.headers.get("x-request-id")
        request.state.request_id = incoming or new_request_id()
        try:
            response = await call_next(request)
        except Exception:
            logger.exception(
                "[%s] unhandled error on %s %s",
                request.state.request_id,
                request.method,
                request.url.path,
            )
            raise
        response.headers["x-request-id"] = request.state.request_id
        return response

    def _rid(request: Request) -> str:
        return getattr(request.state, "request_id", new_request_id())

    @app.get("/healthz")
    def healthz() -> Dict[str, Any]:
        return {"status": "ok"}

    @app.post("/api/datasets", response_model=IngestResponse, status_code=201)
    def ingest_dataset(payload: IngestRequest, request: Request) -> Any:
        assert holder.service is not None
        result = holder.service.ingest_dataset(
            name=payload.name,
            raw_transactions=payload.transactions,
            min_support=payload.min_support,
            overwrite=payload.overwrite,
            max_length=payload.max_length,
            request_id=_rid(request),
        )
        return IngestResponse(
            dataset_id=result.dataset_id,
            dataset_name=result.dataset_name,
            n_transactions=result.n_transactions,
            n_itemsets=result.n_itemsets,
            diagnostics=result.diagnostics,
        )

    @app.get("/api/datasets")
    def list_datasets() -> Dict[str, Any]:
        assert holder.db is not None
        return {"datasets": holder.db.list_datasets()}

    @app.post("/api/datasets/{name}/rules", response_model=RuleResponse)
    def generate(payload: RuleRequest, name: str, request: Request) -> Any:
        assert holder.service is not None
        result = holder.service.audit_rules_raw(
            name,
            min_confidence=payload.min_confidence,
            min_lift=payload.min_lift,
            min_leverage=payload.min_leverage,
            max_rules=payload.max_rules,
            antecedent=payload.antecedent,
            consequent=payload.consequent,
            request_id=_rid(request),
        )
        return RuleResponse(
            dataset_id=result.dataset_id,
            dataset_name=result.dataset_name,
            request_id=result.request_id,
            n_rules=len(result.rules),
            rules=[rule_to_out(r) for r in result.rules],
            diagnostics=result.diagnostics,
        )

    @app.exception_handler(QueryValidationError)
    async def handle_validation_error(
        request: Request, exc: QueryValidationError
    ) -> JSONResponse:
        rid = _rid(request)
        body = ErrorResponse(
            request_id=rid,
            error="request_out_of_domain",
            issues=[
                {"field": i.field, "reason": i.reason.value, "detail": i.detail}
                for i in exc.issues
            ],
        )
        return JSONResponse(status_code=422, content=body.model_dump())

    @app.exception_handler(KeyError)
    async def handle_not_found(request: Request, exc: KeyError) -> JSONResponse:
        rid = _rid(request)
        return JSONResponse(
            status_code=404,
            content=ErrorResponse(
                request_id=rid, error="not_found", issues=[]
            ).model_dump()
            | {"detail": str(exc).strip('\"')},
        )

    @app.exception_handler(ValueError)
    async def handle_value_error(request: Request, exc: ValueError) -> JSONResponse:
        rid = _rid(request)
        logger.warning("[%s] rejected: %s", rid, exc)
        return JSONResponse(
            status_code=400,
            content=ErrorResponse(
                request_id=rid, error="invalid_corpus_or_parameters", issues=[]
            ).model_dump()
            | {"detail": str(exc)},
        )

    return app


app = create_app()
