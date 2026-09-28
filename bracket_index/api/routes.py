"""HTTP routes. Each handler logs its decision with the request id."""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..diagnostics import (
    DECISION_ACCEPTED,
    DECISION_UNDECIDABLE,
    fingerprint,
    log_decision,
)
from ..index.engine import IndexEngine
from .schemas import (
    BalanceResponse,
    DocumentCreateRequest,
    DocumentCreateResponse,
    DocumentStateResponse,
    EditRequest,
    EditResponse,
    IntervalModel,
    MatchResponse,
    UnbalancedIntervalResponse,
)
from .validation import request_id_of


def build_router(engine: IndexEngine) -> APIRouter:
    router = APIRouter()

    @router.post("/documents", response_model=DocumentCreateResponse, status_code=201)
    def create_document(
        body: DocumentCreateRequest, request: Request
    ) -> DocumentCreateResponse:
        doc_id = engine.create_document(body.text)
        log_decision(
            request_id_of(request),
            DECISION_ACCEPTED,
            "document indexed",
            doc_id=doc_id,
            content=fingerprint(body.text),
        )
        return DocumentCreateResponse(doc_id=doc_id, version=0, length=len(body.text))

    @router.get("/documents/{doc_id}", response_model=DocumentStateResponse)
    def get_document(doc_id: int, request: Request) -> DocumentStateResponse:
        balance = engine.balance(doc_id)
        log_decision(
            request_id_of(request),
            DECISION_ACCEPTED,
            "document state read",
            doc_id=doc_id,
            version=balance.version,
        )
        return DocumentStateResponse(
            doc_id=doc_id, version=balance.version, length=engine.length_of(doc_id)
        )

    @router.get("/documents/{doc_id}/balance", response_model=BalanceResponse)
    def get_balance(doc_id: int, request: Request) -> BalanceResponse:
        result = engine.balance(doc_id)
        log_decision(
            request_id_of(request),
            DECISION_ACCEPTED if result.balanced else DECISION_UNDECIDABLE,
            "document balanced"
            if result.balanced
            else "document not balanced; see interval",
            doc_id=doc_id,
            version=result.version,
            category=result.category,
        )
        return BalanceResponse(
            doc_id=result.doc_id,
            version=result.version,
            balanced=result.balanced,
            category=result.category,
            interval=_interval_model(result.interval),
            unmatched_openers=result.unmatched_openers,
            unmatched_closers=result.unmatched_closers,
            mismatches=result.mismatches,
        )

    @router.get(
        "/documents/{doc_id}/unbalanced-interval",
        response_model=UnbalancedIntervalResponse,
    )
    def get_unbalanced_interval(
        doc_id: int, request: Request
    ) -> UnbalancedIntervalResponse:
        result = engine.balance(doc_id)
        log_decision(
            request_id_of(request),
            DECISION_ACCEPTED if result.balanced else DECISION_UNDECIDABLE,
            "no unbalanced interval"
            if result.balanced
            else f"shortest unbalanced interval: {result.category}",
            doc_id=doc_id,
            version=result.version,
            interval=(
                None
                if result.interval is None
                else [result.interval.start, result.interval.end]
            ),
        )
        return UnbalancedIntervalResponse(
            doc_id=doc_id,
            version=result.version,
            balanced=result.balanced,
            interval=_interval_model(result.interval),
        )

    @router.get("/documents/{doc_id}/match", response_model=MatchResponse)
    def get_match(doc_id: int, pos: int, request: Request) -> MatchResponse:
        result = engine.match(doc_id, pos)
        log_decision(
            request_id_of(request),
            DECISION_ACCEPTED if result.category == "MATCHED" else DECISION_UNDECIDABLE,
            "bracket matched"
            if result.category == "MATCHED"
            else f"no match: {result.category}",
            doc_id=doc_id,
            pos=pos,
            category=result.category,
        )
        return MatchResponse(
            doc_id=result.doc_id,
            pos=result.pos,
            category=result.category,
            match_pos=result.match_pos,
        )

    @router.post("/documents/{doc_id}/edits", response_model=EditResponse)
    def apply_edit(
        doc_id: int, body: EditRequest, request: Request
    ) -> EditResponse:
        result = engine.apply_edit(
            doc_id,
            expected_version=body.expected_version,
            start=body.start,
            end=body.end,
            replacement=body.replacement,
        )
        log_decision(
            request_id_of(request),
            DECISION_ACCEPTED,
            "edit applied",
            doc_id=doc_id,
            version=result.version,
            range=[body.start, body.end],
            replacement=fingerprint(body.replacement),
            rescanned_chunks=result.rescanned_chunks,
            total_chunks=result.total_chunks,
        )
        return EditResponse(
            doc_id=result.doc_id,
            version=result.version,
            length=result.length,
            rescanned_chunks=result.rescanned_chunks,
            total_chunks=result.total_chunks,
        )

    return router


def _interval_model(interval) -> IntervalModel | None:
    if interval is None:
        return None
    return IntervalModel(
        start=interval.start, end=interval.end, category=interval.category
    )
