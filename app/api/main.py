"""FastAPI application: thin HTTP adapter over AuditService."""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..config import Settings, get_settings
from ..corpus.schema import CorpusError
from ..diagnostics import get_request_id, new_request_id, set_request_id
from ..index.db import Store
from ..index.models import (
    ApiError,
    CorpusIn,
    CorpusOut,
    ItemsetOut,
    MineRequest,
    MineResult,
    RuleOut,
    RuleQuery,
    RuleEvaluateRequest,
    RuleSetOut,
)
from ..mining.rules import Rule
from ..service import AuditService
from ..validation.queries import QueryRejected

logging.basicConfig(level=logging.INFO)


def _rule_out(rule: Rule) -> RuleOut:
    return RuleOut(
        rule_id=rule.rule_id,
        antecedent=sorted(rule.antecedent),
        consequent=sorted(rule.consequent),
        support=rule.metrics.support,
        confidence=rule.metrics.confidence,
        lift=rule.metrics.lift,
        leverage=rule.metrics.leverage,
        warnings=rule.warnings,
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    service = AuditService(Store(settings.db_path), settings)

    app = FastAPI(title="Association Rule Lift Audit", version="0.1.0")
    app.state.service = service

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        rid = request.headers.get("X-Request-ID") or new_request_id()
        set_request_id(rid)
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        return response

    @app.exception_handler(QueryRejected)
    async def query_rejected_handler(request: Request, exc: QueryRejected):
        status = 404 if exc.category == "CORPUS_NOT_FOUND" else 422
        return JSONResponse(
            status_code=status,
            content=ApiError(
                category=exc.category, detail=exc.detail,
                request_id=get_request_id(),
            ).model_dump(),
        )

    @app.exception_handler(CorpusError)
    async def corpus_error_handler(request: Request, exc: CorpusError):
        return JSONResponse(
            status_code=422,
            content=ApiError(
                category=exc.category, detail=exc.detail,
                request_id=get_request_id(),
            ).model_dump(),
        )

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/v1/corpora", response_model=CorpusOut, status_code=201)
    def create_corpus(body: CorpusIn) -> CorpusOut:
        loaded = service.create_corpus(
            body.name, [(t.transaction_id, t.items) for t in body.transactions]
        )
        return CorpusOut(
            corpus_id=loaded.corpus_id,
            name=loaded.name,
            n_transactions=loaded.n_transactions,
            n_distinct_items=loaded.n_distinct_items,
            n_duplicate_items_removed=loaded.n_duplicate_items_removed,
            request_id=get_request_id(),
        )

    @app.post("/v1/corpora/{corpus_id}/mine", response_model=MineResult)
    def mine(corpus_id: int, body: MineRequest) -> MineResult:
        itemsets = service.mine(corpus_id, body.min_support)
        return MineResult(
            corpus_id=corpus_id,
            min_support=body.min_support,
            n_itemsets=len(itemsets),
            itemsets=[ItemsetOut(**i) for i in itemsets],
            request_id=get_request_id(),
        )

    @app.post("/v1/corpora/{corpus_id}/rules", response_model=RuleSetOut)
    def rules(corpus_id: int, body: RuleQuery) -> RuleSetOut:
        generated = service.generate_rules(corpus_id, body.min_confidence, body.min_lift)
        return RuleSetOut(
            corpus_id=corpus_id,
            n_rules=len(generated),
            rules=[_rule_out(r) for r in generated],
            request_id=get_request_id(),
        )

    @app.post("/v1/corpora/{corpus_id}/rules/evaluate", response_model=RuleOut)
    def evaluate(corpus_id: int, body: RuleEvaluateRequest) -> RuleOut:
        rule = service.evaluate(corpus_id, body.antecedent, body.consequent)
        return _rule_out(rule)

    return app


app = create_app()
