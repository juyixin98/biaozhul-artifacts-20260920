"""HTTP interface: thin FastAPI router over the application service.

The router does no polynomial work; it validates the schema, hands the raw
coefficient list to :func:`app.service.isolate_coefficients`, and serializes
the result. Transport-level concerns (request id, body size, redacted access
logs) live in :func:`create_app`.
"""
from __future__ import annotations

from fastapi import APIRouter, Request

from .schemas import IsolationRequest, IsolationResponse
from .service import isolate_coefficients

router = APIRouter()


def get_settings(request: Request):
    return request.app.state.settings


@router.post(
    "/api/v1/isolate-real-roots",
    response_model=IsolationResponse,
    summary="Isolate the distinct real roots of a rational-coefficient polynomial",
)
def isolate_roots(
    payload: IsolationRequest,
    request: Request,
) -> IsolationResponse:
    # Synchronous handler: exact isolation is CPU-bound and may run long, so
    # FastAPI executes it in its worker threadpool instead of blocking the
    # event loop (no async I/O happens inside the kernel).
    settings = get_settings(request)
    # Prefer an explicit client id, then the middleware-correlated id.
    request_id = payload.request_id or request.scope.get("correlation_request_id")
    result = isolate_coefficients(
        coefficients=payload.coefficients,
        settings=settings,
        target_width=payload.target_width,
        interval_lo=payload.interval_lo,
        interval_hi=payload.interval_hi,
        request_id=request_id,
        include_evidence=payload.include_evidence,
    )
    # The service outcome is explicit; HTTP stays 200 for a well-formed call
    # and clients branch on ``status`` (ok / zero_polynomial / undetermined /
    # rejected). Malformed JSON/schema is rejected earlier by FastAPI with 422.
    return IsolationResponse(
        request_id=result.request_id,
        status=result.status,
        degree=result.degree,
        is_zero_polynomial=result.is_zero_polynomial,
        roots=result.roots,
        multiplicities=result.multiplicities,
        evidence=result.evidence,
        diagnostics=result.diagnostics,
        failure=result.failure,
    )


@router.get("/health", summary="Liveness probe")
async def health() -> dict[str, str]:
    return {"status": "up"}
