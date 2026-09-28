"""FastAPI application: corpus ingestion, batch LCS queries, health."""

from __future__ import annotations

import base64
import logging
import threading
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.config import APP_VERSION, INDEX_FORMAT_VERSION, Settings, load_settings
from app.corpus import decode_document, validate_documents
from app.errors import AppError, FailureCategory
from app.index import Index, build_index, locate, mine_longest
from app.logging_setup import configure_logging, new_request_id, request_id_var
from app.models import (
    BatchQueryRequest,
    BatchQueryResponse,
    CandidateOut,
    CorpusResponse,
    CreateCorpusRequest,
    HealthResponse,
    OccurrenceOut,
    QueryResult,
)
from app.store import CorpusStore
from app.validation import resolve_max_candidates, validate_min_docs

logger = logging.getLogger("lcs.service")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    configure_logging()
    store = CorpusStore(settings.db_path)
    index_cache: dict[str, Index] = {}
    cache_lock = threading.Lock()

    app = FastAPI(title="multi-doc-lcs", version=APP_VERSION)
    app.state.settings = settings

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or new_request_id()
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
            elapsed_ms = (time.perf_counter() - started) * 1000
            logger.info(
                "step=http.request method=%s path=%s status=%d elapsed_ms=%.1f",
                request.method,
                request.url.path,
                response.status_code,
                elapsed_ms,
            )
            response.headers["x-request-id"] = request_id
            return response
        finally:
            request_id_var.reset(token)

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        status = 404 if exc.category is FailureCategory.CORPUS_NOT_FOUND else 400
        logger.warning(
            "step=request.rejected category=%s detail=%s", exc.category.value, exc.detail
        )
        return JSONResponse(
            status_code=status,
            content={
                "error": {
                    "category": exc.category.value,
                    "detail": exc.detail,
                    "request_id": request_id_var.get(),
                }
            },
        )

    def get_index(corpus_id: str) -> Index:
        with cache_lock:
            cached = index_cache.get(corpus_id)
        if cached is not None:
            return cached
        started = time.perf_counter()
        index = store.load_index(corpus_id)
        logger.info(
            "step=index.loaded corpus_id=%s n_symbols=%d elapsed_ms=%.1f",
            corpus_id,
            index.total_symbols,
            (time.perf_counter() - started) * 1000,
        )
        with cache_lock:
            index_cache[corpus_id] = index
        return index

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(
            status="ok", app_version=APP_VERSION, index_version=INDEX_FORMAT_VERSION
        )

    @app.post("/corpora", response_model=CorpusResponse, status_code=201)
    def create_corpus(payload: CreateCorpusRequest) -> CorpusResponse:
        docs = [decode_document(d.doc_id, d.content_b64) for d in payload.documents]
        validate_documents(docs, settings)
        logger.info(
            "step=corpus.validated doc_count=%d total_bytes=%d",
            len(docs),
            sum(len(d.content) for d in docs),
        )
        started = time.perf_counter()
        index = build_index(docs)
        logger.info(
            "step=index.built n_symbols=%d elapsed_ms=%.1f",
            index.total_symbols,
            (time.perf_counter() - started) * 1000,
        )
        meta = store.save_corpus(payload.name, docs, index)
        with cache_lock:
            index_cache[meta["corpus_id"]] = index
        logger.info("step=corpus.stored corpus_id=%s", meta["corpus_id"])
        return CorpusResponse(request_id=request_id_var.get(), **meta)

    @app.get("/corpora/{corpus_id}", response_model=CorpusResponse)
    def describe_corpus(corpus_id: str) -> CorpusResponse:
        meta = store.get_corpus_meta(corpus_id)
        return CorpusResponse(request_id=request_id_var.get(), **meta)

    @app.post("/corpora/{corpus_id}/queries", response_model=BatchQueryResponse)
    def run_queries(corpus_id: str, payload: BatchQueryRequest) -> BatchQueryResponse:
        meta = store.get_corpus_meta(corpus_id)
        index = get_index(corpus_id)
        results: list[QueryResult] = []
        for spec in payload.queries:
            results.append(_run_one(index, spec, settings))
        return BatchQueryResponse(
            corpus_id=corpus_id,
            request_id=request_id_var.get(),
            app_version=APP_VERSION,
            index_version=meta["index_version"],
            results=results,
        )

    return app


def _run_one(index: Index, spec, settings: Settings) -> QueryResult:
    """Run one query spec; failures are reported per query, never silently."""
    try:
        validate_min_docs(spec.min_docs, index.doc_count)
        max_candidates = resolve_max_candidates(spec.max_candidates, settings)
    except AppError as exc:
        logger.warning(
            "step=query.rejected query_id=%s category=%s detail=%s",
            spec.query_id,
            exc.category.value,
            exc.detail,
        )
        return QueryResult(
            query_id=spec.query_id,
            status="error",
            failure_category=exc.category.value,
            detail=exc.detail,
            min_docs=spec.min_docs,
        )

    started = time.perf_counter()
    length, substrings = mine_longest(index, spec.min_docs)
    elapsed_ms = (time.perf_counter() - started) * 1000
    if length == 0:
        logger.info(
            "step=query.empty query_id=%s min_docs=%d elapsed_ms=%.1f",
            spec.query_id,
            spec.min_docs,
            elapsed_ms,
        )
        return QueryResult(
            query_id=spec.query_id,
            status="no_result",
            detail="no non-empty substring covers the requested number of documents",
            min_docs=spec.min_docs,
        )

    truncated = len(substrings) > max_candidates
    shown = substrings[:max_candidates]
    candidates: list[CandidateOut] = []
    for rank, substring in enumerate(shown):
        candidate, occ_truncated = locate(index, substring, settings.occurrence_cap)
        candidates.append(
            CandidateOut(
                rank=rank,
                length=length,
                substring_b64=base64.b64encode(substring).decode("ascii"),
                substring_hex=substring.hex(),
                doc_coverage=list(candidate.doc_coverage),
                doc_coverage_count=len(candidate.doc_coverage),
                occurrences=[
                    OccurrenceOut(doc_id=o.doc_id, offset=o.offset)
                    for o in candidate.occurrences
                ],
                occurrences_truncated=occ_truncated,
            )
        )
    logger.info(
        "step=query.executed query_id=%s min_docs=%d length=%d candidates=%d "
        "truncated=%s elapsed_ms=%.1f",
        spec.query_id,
        spec.min_docs,
        length,
        len(substrings),
        truncated,
        elapsed_ms,
    )
    return QueryResult(
        query_id=spec.query_id,
        status="ok",
        min_docs=spec.min_docs,
        length=length,
        candidate_count=len(substrings),
        candidates_truncated=truncated,
        candidates=candidates,
    )


app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)
