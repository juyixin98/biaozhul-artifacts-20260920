"""FastAPI HTTP interface.

Thin layer: parses/validates the wire format, delegates to SolverService,
maps the error taxonomy to HTTP statuses. No numeric logic lives here.
"""

from __future__ import annotations

import os
from typing import List, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from .domain import (
    DEFAULT_CLUSTER_TOL,
    DEFAULT_CONV_TOL,
    DEFAULT_MAX_DEGREE,
    DEFAULT_MAX_ITER,
    DEFAULT_PAIR_TOL,
    HARD_MAX_ITER,
    Method,
    SolveOptions,
)
from .errors import HTTP_STATUS, PolyRootsError
from .runlog import RunLogger
from .service import RunRegistry, SolverService


class OptionsModel(BaseModel):
    method: Method = Method.COMPANION_ABERTH
    max_iter: int = Field(default=DEFAULT_MAX_ITER, ge=0, le=HARD_MAX_ITER)
    conv_tol: float = Field(default=DEFAULT_CONV_TOL, gt=0.0, le=1.0)
    cluster_tol: float = Field(default=DEFAULT_CLUSTER_TOL, gt=0.0, le=1.0)
    pair_tol: float = Field(default=DEFAULT_PAIR_TOL, gt=0.0, le=1.0)
    max_degree: int = Field(default=DEFAULT_MAX_DEGREE, ge=1, le=10_000)


class SolveRequest(BaseModel):
    # Descending powers: coefficients[0] multiplies z**degree.
    coefficients: List[List[float]] = Field(min_length=1)
    options: OptionsModel = OptionsModel()
    run_id: Optional[str] = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")

    @field_validator("coefficients")
    @classmethod
    def _pairs_must_be_re_im(cls, value):
        for i, pair in enumerate(value):
            if len(pair) != 2:
                raise ValueError(f"coefficient {i} must be a [re, im] pair")
        return value


def create_app(log_dir: Optional[str] = None) -> FastAPI:
    log_dir = log_dir or os.environ.get("POLYROOOTS_LOG_DIR", "logs")
    service = SolverService(registry=RunRegistry(), logger=RunLogger(log_dir))

    app = FastAPI(title="polyroots", version="0.1.0")
    app.state.service = service

    @app.exception_handler(PolyRootsError)
    async def _polyroots_error_handler(_: Request, exc: PolyRootsError) -> JSONResponse:
        return JSONResponse(
            status_code=HTTP_STATUS[exc.category],
            content={"error": exc.to_dict()},
        )

    @app.get("/v1/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/v1/solve")
    def solve(req: SolveRequest) -> dict:
        options = SolveOptions(
            method=req.options.method,
            max_iter=req.options.max_iter,
            conv_tol=req.options.conv_tol,
            cluster_tol=req.options.cluster_tol,
            pair_tol=req.options.pair_tol,
            max_degree=req.options.max_degree,
        )
        return service.solve(req.coefficients, options, req.run_id)

    @app.get("/v1/runs/{run_id}")
    def get_run(run_id: str) -> JSONResponse:
        report = service.registry.get(run_id)
        if report is None:
            return JSONResponse(
                status_code=404,
                content={"error": {"category": "not_found",
                                   "message": f"unknown run_id {run_id!r}",
                                   "run_id": run_id}},
            )
        return JSONResponse(content=report)

    return app


app = create_app()
