"""FastAPI service interface for the Toeplitz FFT backend.

Errors are never collapsed into success: backend errors map to HTTP 400 with
a stable ``error.category`` matching the exception taxonomy in errors.py, and
request-shape problems surface as HTTP 422 from pydantic validation.
"""

from __future__ import annotations

import uuid

import numpy as np
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .cache import PlanCache
from .config import ToeplitzConfig
from .errors import ToeplitzError
from .evidence import runtime_metadata
from .kernel import matmat, matvec
from .schemas import (
    MatmatRequest,
    MatmatResponse,
    MatvecRequest,
    MatvecResponse,
    ResponseMeta,
    decode_vector,
    encode_vector,
)


def create_app(config: ToeplitzConfig | None = None) -> FastAPI:
    config = config or ToeplitzConfig.from_env()
    app = FastAPI(title="toeplitz-fft", version="1.0.0")
    app.state.config = config
    app.state.cache = PlanCache(maxsize=config.cache_maxsize)

    @app.exception_handler(ToeplitzError)
    async def toeplitz_error_handler(request: Request, exc: ToeplitzError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={"error": {"category": exc.category, "message": str(exc)}},
        )

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.get("/v1/meta")
    async def meta() -> dict:
        return {
            "versions": runtime_metadata(),
            "config": {
                "real_dtype": config.real_dtype,
                "complex_dtype": config.complex_dtype,
                "pad_to_power_of_two": config.pad_to_power_of_two,
                "cache_maxsize": config.cache_maxsize,
            },
            "cache": app.state.cache.stats(),
        }

    @app.post("/v1/toeplitz/matvec", response_model=MatvecResponse)
    async def matvec_endpoint(req: MatvecRequest) -> MatvecResponse:
        c = decode_vector(req.c)
        r = decode_vector(req.r)
        x = decode_vector(req.x)
        y, info = matvec(c, r, x, mode=req.mode, config=config, cache=app.state.cache)
        return MatvecResponse(
            y=encode_vector(y),
            meta=ResponseMeta(request_id=uuid.uuid4().hex, **info),
        )

    @app.post("/v1/toeplitz/matmat", response_model=MatmatResponse)
    async def matmat_endpoint(req: MatmatRequest) -> MatmatResponse:
        c = decode_vector(req.c)
        r = decode_vector(req.r)
        columns = [decode_vector(col) for col in req.X]
        X = np.stack(columns, axis=1)  # (n, k)
        Y, info = matmat(c, r, X, mode=req.mode, config=config, cache=app.state.cache)
        return MatmatResponse(
            Y=[encode_vector(Y[:, j]) for j in range(Y.shape[1])],
            meta=ResponseMeta(request_id=uuid.uuid4().hex, **info),
        )

    return app


app = create_app()
