"""FastAPI service interface.

Endpoints
----------
``GET  /health``         liveness plus run identity
``GET  /metadata``       versions, kernel digest, settings, cache info
``POST /compute``        Toeplitz multiply (real/complex, single/double)
``POST /verify``         compute AND produce independent oracle evidence

Errors are returned with an explicit failure category and HTTP status;
an exception never produces an ``ok: true`` envelope.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import __version__
from .engine import Engine
from .errors import ErrorCode, InputError, ToeplitzError
from .evidence import verify_result
from .inputs import parse_problem
from .logging_ctx import (
    configure_logging,
    environment_versions,
    get_logger,
    get_run_id,
    new_run_id,
    set_run_id,
)

configure_logging()
LOG = get_logger()
ENGINE = Engine()


def _scalar(value: Any) -> Any:
    if isinstance(value, (np.floating, np.integer)):
        return value.item()
    if isinstance(value, np.complexfloating):
        return [float(value.real), float(value.imag)]
    return value


def serialize_output(arr: np.ndarray) -> list[Any]:
    """Serialize ``(batch, n)`` result: real -> floats, complex -> pairs."""
    if np.iscomplexobj(arr):
        return [[[float(z.real), float(z.imag)] for z in row] for row in arr]
    return [[float(v) for v in row] for row in arr]


def create_app() -> FastAPI:
    app = FastAPI(
        title="toeplitz-fft-backend",
        version=__version__,
        description="Toeplitz matrix-vector / batched multiply via "
                    "circulant embedding and FFT, with independent evidence.",
    )

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "run_id": get_run_id(), "version": __version__}

    @app.get("/metadata")
    def metadata() -> dict[str, Any]:
        return {
            "versions": environment_versions(),
            "kernel": ENGINE.kernel_summary(),
            "cache": ENGINE.cache_info(),
            "settings": {
                "max_n": ENGINE._settings.max_n,  # noqa: SLF001
                "max_batch": ENGINE._settings.max_batch,
                "cache_size": ENGINE._settings.cache_size,
                "tiny_n_explicit": ENGINE._settings.tiny_n_explicit,
                "oracle_mpmath_prec": ENGINE._settings.oracle_mpmath_prec,
            },
        }

    @app.post("/compute")
    async def compute(request: Request) -> JSONResponse:
        run_id = set_run_id(new_run_id("req"))
        LOG.info("http endpoint=/compute run_id=%s", run_id)
        try:
            payload = await request.json()
        except Exception as exc:  # malformed JSON body
            LOG.warning("http bad_json run_id=%s err=%s", run_id, exc)
            err = InputError(ErrorCode.BAD_REQUEST,
                             f"request body must be JSON: {exc}")
            return JSONResponse(err.to_dict(), status_code=400)
        try:
            problem = parse_problem(payload)
            result = ENGINE.run(problem)
        except ToeplitzError as exc:
            LOG.warning("http categorized_error run_id=%s code=%s",
                        run_id, exc.code.value)
            status = 422 if isinstance(exc, InputError) else 500
            body = exc.to_dict()
            body["run_id"] = run_id
            return JSONResponse(body, status_code=status)

        body: dict[str, Any] = {
            "ok": True,
            "run_id": run_id,
            "result": serialize_output(result.output),
            "plan": {
                "n": result.plan.n,
                "batch": result.plan.batch,
                "embedding_m": result.plan.m,
                "mode": result.plan.mode,
                "precision": result.plan.precision,
                "dtype": result.plan.dtype,
                "path": result.plan.path,
                "coefficient_digest": result.plan.coefficient_digest,
                "kernel_digest": result.plan.kernel_digest,
                "kernel_name": result.plan.kernel_name,
                "kernel_version": result.plan.kernel_version,
            },
            "cache_hit": result.cache_hit,
            "stats": result.stats,
        }
        return JSONResponse(body)

    @app.post("/verify")
    async def verify(request: Request) -> JSONResponse:
        run_id = set_run_id(new_run_id("verify"))
        LOG.info("http endpoint=/verify run_id=%s", run_id)
        try:
            payload = await request.json()
        except Exception as exc:
            err = InputError(ErrorCode.BAD_REQUEST,
                             f"request body must be JSON: {exc}")
            return JSONResponse({**err.to_dict(), "run_id": run_id},
                                status_code=400)
        try:
            problem = parse_problem(payload)
            result = ENGINE.run(problem)
            report = verify_result(
                problem,
                result.output.astype(
                    np.complex128 if problem.is_complex else np.float64),
                cache_hit=result.cache_hit,
                kernel_path=result.plan.path,
                memory=result.stats.get("memory", {}),
                case_id=run_id,
            )
        except ToeplitzError as exc:
            status = 422 if isinstance(exc, InputError) else 500
            return JSONResponse({**exc.to_dict(), "run_id": run_id},
                                status_code=status)

        body = {
            "ok": report.passed,
            "run_id": run_id,
            "result": serialize_output(result.output),
            "evidence": report.to_dict(),
        }
        # Numeric failure is an explicit state, never collapsed to 200/ok.
        return JSONResponse(body, status_code=200 if report.passed else 422)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        run_id = get_run_id()
        LOG.exception("http unhandled_exception run_id=%s", run_id)
        return JSONResponse(
            {
                "ok": False,
                "run_id": run_id,
                "error": {
                    "code": ErrorCode.INTERNAL_ERROR.value,
                    "message": f"unhandled error: {exc.__class__.__name__}",
                    "details": {},
                },
            },
            status_code=500,
        )

    return app


app = create_app()
