"""Thin HTTP router delegating all work to the service layer."""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..core.config import CertConfig
from ..core.errors import InvalidRequest
from ..core.tracer import Tracer
from ..services import certify_service
from .schemas import CertifyRequest, CertifyResponse, ErrorResponse

router = APIRouter(tags=["certify"])

_ERROR_RESPONSES = {
    400: {"model": ErrorResponse, "description": "input / parse / validation error"},
    409: {"model": ErrorResponse, "description": "conflicting parameters"},
    422: {"model": ErrorResponse, "description": "expression leaves the real domain"},
    500: {"model": ErrorResponse, "description": "unexpected computation failure"},
}


def _build_config(request: CertifyRequest, base: CertConfig) -> CertConfig:
    precision = request.precision_dps or base.precision_dps
    # Display digits must stay strictly below working precision; adapt the
    # default downward when a caller requests a lower precision.
    display_digits = max(1, min(base.display_digits, precision - 10))
    try:
        return CertConfig(
            precision_dps=precision,
            target_width=request.target_width or base.target_width,
            max_depth=request.max_depth or base.max_depth,
            max_evals=request.max_evals or base.max_evals,
            max_newton_iters=base.max_newton_iters,
            max_expression_len=base.max_expression_len,
            display_digits=display_digits,
        ).validated()
    except ValueError as exc:
        raise InvalidRequest(str(exc)) from exc


@router.post(
    "/certify",
    response_model=CertifyResponse,
    responses=_ERROR_RESPONSES,
    summary="Certify roots of an expression over an interval",
)
async def certify_roots(http_request: Request, request: CertifyRequest) -> dict:
    base: CertConfig = http_request.app.state.default_config
    log_dir: str | None = http_request.app.state.log_dir
    config = _build_config(request, base)

    with Tracer.create(log_dir) as tracer:
        payload = certify_service.certify_expression(
            expression=request.expression,
            lower=request.lower,
            upper=request.upper,
            config=config,
            tracer=tracer,
            include_approximation=request.include_approximation,
            sample_count=request.sample_count,
        )
        payload["trace"] = tracer.summary()
    return payload
