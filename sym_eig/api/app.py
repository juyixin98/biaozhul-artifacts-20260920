"""FastAPI application exposing the eigendecomposition service."""

import logging

from fastapi import FastAPI, Header, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from sym_eig.api.schemas import EigenRequest, EigenResponseOut, HealthOut
from sym_eig.config import Settings
from sym_eig.errors import HTTP_STATUS, EigServiceError, ErrorCategory
from sym_eig.service.engine import RequestOptions, run_eigendecomposition
from sym_eig.service.tracing import configure_logging, set_request_id
from sym_eig.version import ALGORITHM_NAME, __version__

# Per-element worst-case token allowance used only to cap the accepted body
# size before parsing, so the dimension budget also bounds parse-time memory.
_BYTES_PER_ENTRY = 64
_BODY_OVERHEAD_BYTES = 1 << 16


def _error_envelope(
    request_id: str,
    category: ErrorCategory,
    message: str,
    details: dict | None = None,
) -> dict:
    return {
        "request_id": request_id,
        "verdict": (
            "UNCERTAIN" if category == ErrorCategory.UNCERTAIN_RESULT
            else "FAILED"
        ),
        "dimension": 0,
        "error_category": str(category),
        "error_message": message,
        "error_details": details or {},
        "algorithm": ALGORITHM_NAME,
        "service_version": __version__,
    }


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    logger = configure_logging()
    app = FastAPI(
        title="Symmetric Matrix Eigendecomposition Service",
        version=__version__,
        description=(
            "Eigenvalues/eigenvectors of a real symmetric matrix via "
            "Householder tridiagonalization + implicit Wilkinson QR, with "
            "independent residual, orthogonality and reconstruction evidence."
        ),
    )
    app.state.settings = settings
    body_limit = settings.max_n ** 2 * _BYTES_PER_ENTRY + _BODY_OVERHEAD_BYTES

    @app.middleware("http")
    async def _limit_body_size(request: Request, call_next):
        # Reject oversized bodies from the declared Content-Length before
        # pydantic materializes them, bounding parse-time memory by max_n.
        if request.method == "POST":
            declared = request.headers.get("content-length")
            if declared is not None:
                try:
                    if int(declared) > body_limit:
                        rid = set_request_id(
                            request.headers.get("x-request-id")
                        )
                        return JSONResponse(
                            status_code=422,
                            content=_error_envelope(
                                rid,
                                ErrorCategory.SIZE_LIMIT_EXCEEDED,
                                f"request body ({int(declared)} bytes) exceeds "
                                f"the {body_limit}-byte limit for "
                                f"max_n={settings.max_n}",
                                {"content_length": int(declared),
                                 "body_limit_bytes": body_limit},
                            ),
                        )
                except ValueError:
                    pass
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def _on_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Structural request defects still return the classified envelope so
        # clients have one consistent error shape with a correlation id.
        rid = set_request_id(request.headers.get("x-request-id"))
        errors = jsonable_encoder(exc.errors())
        # Distinguish a bad matrix from a bad option/parameter by field path.
        locations = {
            tuple(str(p) for p in err.get("loc", [])) for err in errors
        }
        matrix_error = any(loc and loc[-1] == "matrix" for loc in locations)
        category = (
            ErrorCategory.INVALID_MATRIX if matrix_error
            else ErrorCategory.INVALID_PARAMETER
        )
        return JSONResponse(
            status_code=422,
            content=_error_envelope(
                rid, category, "request failed schema validation",
                {"validation_errors": errors},
            ),
        )

    @app.exception_handler(EigServiceError)
    async def _on_classified_error(
        request: Request, exc: EigServiceError
    ) -> JSONResponse:
        # Safety net so a classified error raised outside the engine's own
        # mapping still uses the single canonical status table.
        rid = set_request_id(request.headers.get("x-request-id"))
        return JSONResponse(
            status_code=exc.http_status,
            content=_error_envelope(
                rid, exc.category, exc.message, exc.details
            ),
        )

    @app.exception_handler(Exception)
    async def _on_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # Never leak an opaque 500 or internal detail (incl. paths); log the
        # full exception server-side under the correlation id.
        rid = set_request_id(request.headers.get("x-request-id"))
        logger.exception("unhandled error for request %s", rid)
        return JSONResponse(
            status_code=500,
            content=_error_envelope(
                rid,
                ErrorCategory.INVALID_MATRIX,
                "internal error while processing the request",
            ),
        )

    @app.get("/health", response_model=HealthOut)
    def health() -> HealthOut:
        return HealthOut(
            status="ok",
            service_version=__version__,
            algorithm=ALGORITHM_NAME,
            config={
                "max_n": settings.max_n,
                "max_iters": settings.max_iters,
                "symmetry_rtol": settings.symmetry_rtol,
                "residual_rtol": settings.residual_rtol,
                "orthogonality_tol": settings.orthogonality_tol,
                "cluster_rtol": settings.cluster_rtol,
                "reference_max_n": settings.reference_max_n,
                "reference_dps": settings.reference_dps,
                "body_limit_bytes": body_limit,
            },
        )

    @app.post(
        "/api/v1/eigendecomposition",
        response_model=EigenResponseOut,
        responses={422: {"model": EigenResponseOut}},
    )
    def eigendecompose(
        payload: EigenRequest,
        request: Request,
        x_request_id: str | None = Header(default=None),
    ) -> JSONResponse:
        request_id = (
            payload.request_id
            or x_request_id
            or request.headers.get("x-request-id")
        )
        set_request_id(request_id)
        options = RequestOptions(
            request_id=request_id,
            max_iters=payload.max_iters,
            symmetry_rtol=payload.symmetry_rtol,
            symmetry_atol=payload.symmetry_atol,
            residual_rtol=payload.residual_rtol,
            orthogonality_tol=payload.orthogonality_tol,
            reconstruction_rtol=payload.reconstruction_rtol,
            cluster_rtol=payload.cluster_rtol,
            reference=payload.reference,
            include_vectors=payload.include_vectors,
        )
        result = run_eigendecomposition(
            payload.matrix, settings=settings, options=options
        )
        # Single canonical category -> HTTP status table.
        status_code = 200
        if result.verdict == "FAILED" and result.error_category:
            status_code = HTTP_STATUS[
                ErrorCategory(result.error_category)
            ]
        body = result.to_dict()
        return JSONResponse(status_code=status_code, content=body)

    return app


app = create_app()
