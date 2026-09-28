"""Query validation: maps domain errors to HTTP responses with diagnostics.

Every rejection is logged with the request id, the record id, and the key
state (versions, ranges) that forced the decision, so an operator can see
why a request was rejected without seeing any document content.
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from ..diagnostics import DECISION_REJECTED, log_decision
from ..index.errors import (
    CATEGORY_DOCUMENT_NOT_FOUND,
    CATEGORY_INVALID_RANGE,
    CATEGORY_NOT_A_BRACKET,
    CATEGORY_STALE_VERSION,
    DocumentNotFoundError,
    IndexError_,
    InvalidRangeError,
    NotABracketError,
    StaleVersionError,
)

_STATUS_BY_CATEGORY = {
    CATEGORY_DOCUMENT_NOT_FOUND: 404,
    CATEGORY_STALE_VERSION: 409,
    CATEGORY_INVALID_RANGE: 422,
    CATEGORY_NOT_A_BRACKET: 422,
}


def _state_for(exc: IndexError_) -> dict:
    if isinstance(exc, StaleVersionError):
        return {
            "doc_id": exc.doc_id,
            "expected_version": exc.expected,
            "actual_version": exc.actual,
        }
    if isinstance(exc, InvalidRangeError):
        return {"start": exc.start, "end": exc.end, "length": exc.length}
    if isinstance(exc, NotABracketError):
        return {"pos": exc.pos}
    if isinstance(exc, DocumentNotFoundError):
        return {"doc_id": exc.doc_id}
    return {}


def request_id_of(request: Request) -> str:
    return getattr(request.state, "request_id", "unknown")


async def domain_error_handler(request: Request, exc: IndexError_) -> JSONResponse:
    request_id = request_id_of(request)
    category = exc.category
    log_decision(
        request_id,
        DECISION_REJECTED,
        str(exc),
        category=category,
        **_state_for(exc),
    )
    return JSONResponse(
        status_code=_STATUS_BY_CATEGORY.get(category, 400),
        content={
            "error": {
                "category": category,
                "message": str(exc),
                "request_id": request_id,
            }
        },
    )
