"""HTTP interface: thin routers over :mod:`app.service`."""

from __future__ import annotations

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

from .. import __version__
from ..schemas import (
    ErrorResponse,
    HealthResponse,
    SolveRequest,
    SolveResponse,
)
from ..service import SolveService

router = APIRouter()


def get_service(request: Request) -> SolveService:
    return request.app.state.solve_service


@router.get("/health", response_model=HealthResponse, tags=["meta"])
async def health(request: Request) -> HealthResponse:
    return HealthResponse(status="ok", version=__version__)


@router.post(
    "/solve",
    response_model=SolveResponse,
    responses={
        400: {"model": ErrorResponse},
        422: {"model": SolveResponse},
        500: {"model": ErrorResponse},
    },
    tags=["solver"],
)
async def solve(
    payload: SolveRequest,
    request: Request,
    x_request_id: str | None = Header(default=None, alias="X-Request-ID"),
):
    """Solve A X = B with mixed-precision refinement.

    Per-RHS-column backward errors and accept/reject decisions are returned
    independently; a 422 status means the system was solvable but at least one
    column did not meet the requested tolerance (or A is singular).
    """
    service = get_service(request)
    outcome = service.solve(payload.model_dump(), request_id=x_request_id)
    if outcome.http_status != 200:
        # Full evidence envelope for 409 (tolerance not met / singular);
        # short error envelope for 400/500.
        return JSONResponse(status_code=outcome.http_status, content=outcome.result)
    return outcome.result
