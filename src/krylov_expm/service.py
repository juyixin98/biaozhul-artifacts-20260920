"""FastAPI service interface: request identity, logging, failure reporting."""
from __future__ import annotations

import contextvars
import logging
import time
import uuid

import numpy as np
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from scipy.sparse.linalg import norm as sparse_norm

from . import __version__
from .budget import check_budget
from .config import SolverConfig
from .errors import ExpmvFailure, FailureCategory
from .evidence import ErrorEvidence
from .models import ExpmvRequest, ExpmvResponse, FailureInfo
from .propagator import propagate
from .validation import NormalizedInput, validate_and_normalize

_request_id_var = contextvars.ContextVar("krylov_expm_request_id", default="-")


class _RequestIdFilter(logging.Filter):
    def filter(self, record):
        record.request_id = _request_id_var.get()
        return True


def configure_logging(level: int = logging.INFO) -> logging.Logger:
    logger = logging.getLogger("krylov_expm")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s [request_id=%(request_id)s] %(message)s")
        )
        handler.addFilter(_RequestIdFilter())
        logger.addHandler(handler)
    logger.setLevel(level)
    return logger


_STATUS_BY_CATEGORY = {
    FailureCategory.VALIDATION_ERROR: (422, "rejected"),
    FailureCategory.BUDGET_EXCEEDED: (413, "rejected"),
    FailureCategory.NOT_CONVERGED: (200, "not_converged"),
}


def _meta(config: SolverConfig, elapsed: float) -> dict:
    return {
        "version": __version__,
        "config": config.snapshot(),
        "elapsed_seconds": elapsed,
    }


def _trivial_result(normalized: NormalizedInput):
    """Deterministic edge cases: t == 0 returns v, zero vector returns zero."""
    if normalized.trivial_case == "zero_time":
        vec = normalized.vector.copy()
    else:
        vec = np.zeros_like(normalized.vector)
    norm_a = float(sparse_norm(normalized.matrix, 1)) if normalized.matrix.nnz else 0.0
    evidence = ErrorEvidence(
        norm_a_1=norm_a, planned_segments=0, trivial_case=normalized.trivial_case
    )
    return vec, evidence


def _success_response(rid, result_vec, evidence, config, elapsed):
    body = ExpmvResponse(
        request_id=rid,
        status="converged",
        vector=[float(x) for x in result_vec],
        vector_norm=float(np.linalg.norm(result_vec)),
        evidence=evidence.to_dict(),
        meta=_meta(config, elapsed),
    )
    return JSONResponse(content=body.model_dump())


def _failure_response(rid, exc, config, elapsed):
    code, status = _STATUS_BY_CATEGORY.get(exc.category, (500, "rejected"))
    body = ExpmvResponse(
        request_id=rid,
        status=status,
        failure=FailureInfo(
            category=exc.category.value, message=exc.message, details=exc.details
        ),
        meta=_meta(config, elapsed),
    )
    return JSONResponse(status_code=code, content=body.model_dump())


def create_app(config: SolverConfig | None = None):
    config = config or SolverConfig.from_env()
    logger = configure_logging()
    app = FastAPI(title="krylov-expm-service", version=__version__)

    @app.middleware("http")
    async def request_identity(request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        _request_id_var.set(rid)
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        return response

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "version": __version__}

    @app.post("/v1/expmv")
    def expmv(req: ExpmvRequest):
        rid = _request_id_var.get()
        started = time.perf_counter()
        logger.info(
            "expmv request: t=%s tol=%s vector_len=%s nnz=%s",
            req.t, req.tol, len(req.vector), len(req.matrix.data),
        )
        try:
            normalized = validate_and_normalize(req, config)
            budget = check_budget(normalized.matrix.shape[0], config)
            logger.info("budget ok: %s", budget.to_dict())
            if normalized.trivial_case is not None:
                result_vec, evidence = _trivial_result(normalized)
                logger.info("trivial case: %s", normalized.trivial_case)
            else:
                result_vec, evidence = propagate(
                    normalized.matrix, normalized.vector, normalized.t,
                    normalized.tol, config,
                )
            elapsed = time.perf_counter() - started
            logger.info(
                "converged: segments=%d cumulative_error_estimate=%.3e matvecs=%d elapsed=%.3fs",
                evidence.planned_segments, evidence.cumulative_error_estimate,
                evidence.total_matvec_count, elapsed,
            )
            return _success_response(rid, result_vec, evidence, config, elapsed)
        except ExpmvFailure as exc:
            elapsed = time.perf_counter() - started
            logger.warning("failed [%s]: %s", exc.category.value, exc.message)
            return _failure_response(rid, exc, config, elapsed)
        except Exception as exc:  # last-resort guard, logged with request identity
            elapsed = time.perf_counter() - started
            logger.exception("internal error: %s", exc)
            internal = ExpmvFailure(FailureCategory.INTERNAL_ERROR, str(exc))
            return _failure_response(rid, internal, config, elapsed)

    return app


app = create_app()
