"""HTTP API for the typed bracket index.

Routes stay thin: lexical/structural behavior lives in the mining kernel,
accept/reject policy in :mod:`app.validation`, persistence in the service.
Routes are declared ``def`` (not ``async def``) because the service performs
synchronous SQLite work; FastAPI runs them in its worker threadpool so the
event loop is never blocked.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from .dependencies import AppState, FixedWindowLimiter, get_state, request_id
from .diagnostics import ACCEPTED, REJECTED, UNDETERMINED
from .models import (
    AnalyzeRequest,
    AnalyzeResponse,
    CreateDocumentRequest,
    DefectModel,
    DefectsResponse,
    DocumentModel,
    EditRequest,
    EditResponse,
    MatchResponse,
    VerifyResponse,
)
from .service import ServiceError
from .validation import QueryValidator

router = APIRouter()
_write_limiter = FixedWindowLimiter(max_requests=120, window_seconds=60.0)


def _envelope(data: Any, request_id_value: str, outcome: str = ACCEPTED,
              reason: str = "ok") -> dict[str, Any]:
    return {
        "success": outcome != REJECTED,
        "status": outcome,
        "request_id": request_id_value,
        "reason": reason,
        "data": data,
        "error": None,
    }


def _error(request_id_value: str, category: str, message: str,
           state: dict | None, status_code: int) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={
            "success": False,
            "status": REJECTED,
            "request_id": request_id_value,
            "reason": category,
            "data": None,
            "error": {"category": category, "message": message, "state": state or {}},
        },
    )


def _validator(state: AppState) -> QueryValidator:
    return QueryValidator(state.service, state.diagnostics, state.lexer)


def _defect_model(d) -> DefectModel:
    return DefectModel(
        category=d.category,
        type=d.type,
        interval=[d.start, d.end],
        open_offset=d.open_offset,
        close_offset=d.close_offset,
    )


@router.post("/documents", response_model_by_alias=False)
def create_document(
    body: CreateDocumentRequest,
    state: AppState = Depends(get_state),
    rid: str = Depends(request_id),
):
    _write_limiter.check("create")
    try:
        record = state.service.create_document(body.name, body.content)
    except ServiceError as exc:
        state.diagnostics.record(
            "create_document", REJECTED, exc.category,
            {"name_len": len(body.name), "content_len": len(body.content)}, rid,
        )
        raise _error(rid, exc.category, exc.message, None, 409)
    state.diagnostics.record(
        "create_document", ACCEPTED, "created",
        {"doc_id": record.id, "length": record.length, "version": record.version},
        rid,
    )
    data = DocumentModel(
        id=record.id, name=record.name, lexicon_name=record.lexicon_name,
        length=record.length, version=record.version,
    )
    return _envelope(data.model_dump(), rid)


@router.get("/documents")
def list_documents(state: AppState = Depends(get_state),
                   rid: str = Depends(request_id)):
    records = state.service.list_documents()
    data = [
        DocumentModel(
            id=r.id, name=r.name, lexicon_name=r.lexicon_name,
            length=r.length, version=r.version,
        ).model_dump()
        for r in records
    ]
    return _envelope(data, rid)


@router.get("/documents/{doc_id}")
def get_document(doc_id: int, state: AppState = Depends(get_state),
                 rid: str = Depends(request_id)):
    try:
        record = state.service.get(doc_id)
    except ServiceError as exc:
        raise _error(rid, exc.category, exc.message, None, 404)
    data = DocumentModel(
        id=record.id, name=record.name, lexicon_name=record.lexicon_name,
        length=record.length, version=record.version,
    )
    return _envelope(data.model_dump(), rid)


@router.post("/documents/{doc_id}/edits")
def edit_document(
    doc_id: int,
    body: EditRequest,
    state: AppState = Depends(get_state),
    rid: str = Depends(request_id),
):
    _write_limiter.check(f"edit:{doc_id}")
    verdict = _validator(state).validate_edit(
        doc_id, body.start, body.end, body.replacement, body.base_version, rid
    )
    if verdict.outcome == REJECTED:
        status = 409 if verdict.reason == "VERSION_CONFLICT" else 400
        if verdict.reason in ("NOT_FOUND",):
            status = 404
        raise _error(rid, verdict.reason, verdict.reason, verdict.state, status)
    try:
        report = state.service.edit(
            doc_id, body.start, body.end, body.replacement, body.base_version
        )
    except ServiceError as exc:
        raise _error(rid, exc.category, exc.message, None, 400)
    state.diagnostics.record(
        "edit", ACCEPTED, "applied",
        {
            "doc_id": doc_id,
            "version": report.version,
            "window": list(report.window),
            "blocks_removed": report.blocks_removed,
            "blocks_added": report.blocks_added,
            "rescanned_chars": report.rescanned_chars,
            "unrelated_blocks_untouched": True,
        },
        rid,
    )
    data = EditResponse(
        document_id=report.document_id,
        version=report.version,
        length=report.length,
        invalidated_window=list(report.window),
        blocks_removed=report.blocks_removed,
        blocks_added=report.blocks_added,
        blocks_total=report.blocks_total,
        rescanned_chars=report.rescanned_chars,
        mask_blocks_absorbed=report.mask_blocks_absorbed,
    )
    return _envelope(data.model_dump(), rid)


@router.get("/documents/{doc_id}/match")
def match_bracket(
    doc_id: int,
    offset: int = Query(..., ge=0),
    state: AppState = Depends(get_state),
    rid: str = Depends(request_id),
):
    verdict, payload = _validator(state).validate_match(doc_id, offset, rid)
    if verdict.outcome == REJECTED:
        status = 404 if verdict.reason == "NOT_FOUND" else 400
        raise _error(rid, verdict.reason, verdict.reason, verdict.state, status)
    assert payload is not None
    data = MatchResponse(**payload)
    envelope = _envelope(data.model_dump(), rid, verdict.outcome, verdict.reason)
    if verdict.outcome == UNDETERMINED:
        envelope["success"] = True  # answer returned, but flagged partial
    return envelope


@router.get("/documents/{doc_id}/defects")
def document_defects(doc_id: int, state: AppState = Depends(get_state),
                     rid: str = Depends(request_id)):
    try:
        payload = state.service.shortest_unbalanced(doc_id)
    except ServiceError as exc:
        raise _error(rid, exc.category, exc.message, None, 404)
    state.diagnostics.record(
        "defects", ACCEPTED, "computed",
        {"doc_id": doc_id, "balanced": payload["balanced"],
         "defect_count": len(payload["all_defects"])},
        rid,
    )
    shortest_state = payload["defect"]
    return _envelope(
        DefectsResponse(
            balanced=payload["balanced"],
            version=payload["version"],
            length=payload["length"],
            shortest=_defect_model_from_state(shortest_state)
            if shortest_state else None,
            defects=[
                _defect_model_from_state(d) for d in payload["all_defects"]
            ],
        ).model_dump(),
        rid,
    )


def _defect_model_from_state(d: dict) -> DefectModel:
    return DefectModel(
        category=d["category"], type=d["type"], interval=d["interval"],
        open_offset=d["open_offset"], close_offset=d["close_offset"],
    )


@router.get("/documents/{doc_id}/verify")
def verify_document(doc_id: int, state: AppState = Depends(get_state),
                    rid: str = Depends(request_id)):
    try:
        report = state.service.verify_against_full_scan(doc_id)
    except ServiceError as exc:
        raise _error(rid, exc.category, exc.message, None, 404)
    outcome = ACCEPTED if report["agrees"] else UNDETERMINED
    reason = "index_matches_full_stack_scan" if report["agrees"] \
        else "index_and_full_scan_disagree"
    state.diagnostics.record(
        "verify", outcome, reason,
        {"doc_id": doc_id, "disagreements": len(report["disagreements"])}, rid,
    )
    data = VerifyResponse(**report)
    return _envelope(data.model_dump(), rid, outcome, reason)


@router.post("/query/analyze")
def analyze_text(
    body: AnalyzeRequest,
    state: AppState = Depends(get_state),
    rid: str = Depends(request_id),
):
    verdict, result, match_payload = _validator(state).analyze_text(
        body.content, body.offset, rid
    )
    if verdict.outcome == REJECTED:
        raise _error(rid, verdict.reason, verdict.reason, verdict.state, 400)
    shortest = result.shortest_unbalanced_interval()
    data = AnalyzeResponse(
        balanced=result.balanced,
        length=result.length,
        defect_count=len(result.defects()),
        shortest=_defect_model(shortest) if shortest else None,
        defects=[_defect_model(d) for d in result.defects()],
        match_at=MatchResponse(**match_payload) if match_payload else None,
    )
    return _envelope(data.model_dump(), rid, verdict.outcome, verdict.reason)


@router.get("/diagnostics")
def get_diagnostics(state: AppState = Depends(get_state),
                    limit: int = Query(default=50, ge=1, le=512),
                    rid: str = Depends(request_id)):
    return _envelope({"records": state.diagnostics.recent(limit)}, rid)
