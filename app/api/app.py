"""FastAPI application factory and error envelope handling."""

from __future__ import annotations

import os

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from ..core.config import CertConfig
from ..core.errors import (
    CoreError,
    ErrorCategory,
    ErrorCode,
    HTTP_STATUS,
)
from .routes import router as certify_router


def create_app(log_dir: str | None = None) -> FastAPI:
    app = FastAPI(
        title="Interval Root Certification Service",
        version="1.0.0",
        description=(
            "Certifies roots of continuously differentiable restricted "
            "expressions using interval Newton + bisection. Certified "
            "enclosures are separate from unverified numerical approximations."
        ),
    )
    app.state.log_dir = (
        log_dir
        if log_dir is not None
        else os.environ.get("RC_LOG_DIR", os.path.join(os.getcwd(), "logs"))
    )
    app.state.default_config = CertConfig.from_env().validated()

    app.include_router(certify_router)

    @app.exception_handler(CoreError)
    async def _core_error_handler(_: Request, exc: CoreError) -> JSONResponse:
        status_code = HTTP_STATUS[exc.category]
        return JSONResponse(status_code=status_code, content={"error": exc.as_dict()})

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Normalise pydantic 422s into the service's input-error envelope.
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "code": ErrorCode.INVALID_NUMBER.value,
                    "category": ErrorCategory.INPUT.value,
                    "message": "request validation failed",
                    "details": {"fields": jsonable_encoder(exc.errors())},
                }
            },
        )

    @app.get("/health", tags=["meta"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
