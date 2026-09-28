"""FastAPI service interface.

Endpoints:
    GET  /health                 versions and engine status
    POST /api/v1/solve           factorize + solve + evidence
    POST /api/v1/factor          factorize only (returns diagonal D)
    POST /api/v1/orderings/compare  fill report across orderings
    POST /api/v1/fixtures/run    run a built-in synthetic fixture

Errors always return HTTP 200 with a structured ``error`` body for
categorized kernel failures (so callers get the machine-readable code),
and HTTP 422 for malformed requests. Nothing maps an unknown state to
"success".
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .errors import ErrorCode, SparseSpdError
from .numerical_input import fixtures
from .runlog import RunLogger, library_versions
from .service import schemas
from .service.engine import default_engine

_CATEGORY = {
    ErrorCode.INVALID_SHAPE: "input",
    ErrorCode.INVALID_INDICES: "input",
    ErrorCode.DUPLICATE_ENTRIES: "input",
    ErrorCode.NOT_SQUARE: "input",
    ErrorCode.ASYMMETRIC: "input",
    ErrorCode.EMPTY_MATRIX: "input",
    ErrorCode.TOO_LARGE: "input",
    ErrorCode.SIZE_MISMATCH: "input",
    ErrorCode.NON_SPD_PIVOT: "numerical",
    ErrorCode.SINGULAR_PIVOT: "numerical",
    ErrorCode.PATTERN_MISMATCH: "service",
    ErrorCode.UNSUPPORTED: "service",
    ErrorCode.INTERNAL: "internal",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.engine = default_engine
    yield


app = FastAPI(
    title="Sparse SPD Symbolic + Numeric LDL^T Backend",
    version="1.0.0",
    lifespan=lifespan,
)


def _new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def _error_body(exc: SparseSpdError, run_id: str) -> dict:
    code = getattr(exc, "code", ErrorCode.INTERNAL)
    return {
        "status": "error",
        "run_id": run_id,
        "error_code": code.value,
        "error_category": _CATEGORY.get(code, "internal"),
        "message": str(exc),
        "details": _jsonable(getattr(exc, "details", {})),
    }


def _jsonable(obj):
    import numpy as np
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, np.generic):
        return obj.item()
    return obj


def _report_out(r) -> dict:
    return {
        "run_id": r.run_id,
        "n": r.n,
        "ordering": r.ordering,
        "nnz_input": r.nnz_input,
        "nnz_orig_lower": r.nnz_orig_lower,
        "nnz_l": r.nnz_l,
        "fill_entries": r.fill_entries,
        "fill_ratio": r.fill_ratio,
        "elapsed_symbolic": r.elapsed_symbolic,
        "elapsed_numeric": r.elapsed_numeric,
        "elapsed_total": r.elapsed_total,
        "pivot_count": r.pivot_count,
        "pattern_reused": r.pattern_reused,
        "pattern_fingerprint": r.pattern_fingerprint,
        "ordering_fill_est": r.ordering_fill_est,
    }


def _evidence_out(e) -> dict:
    import math
    return {
        "residual_abs": e.residual_abs,
        "residual_rel": e.residual_rel,
        "reconstruction_abs": e.reconstruction_abs,
        "dense_solution_error": None
        if e.dense_solution_error is not None
        and math.isnan(e.dense_solution_error)
        else e.dense_solution_error,
        "mpmath_solution_error": e.mpmath_solution_error,
        "fill_ratio": e.fill_ratio,
        "passed": e.passed,
        "reasons": list(e.reasons),
    }


@app.get("/health")
def health():
    return {"status": "ok", "versions": library_versions(),
            "cache_entries": len(default_engine.cache or [])}


@app.post("/api/v1/solve")
def solve(req: schemas.SolveRequest):
    run_id = _new_run_id()
    m = req.matrix
    try:
        result = default_engine.solve(
            m.n, m.rows, m.cols, m.values, req.rhs,
            ordering=req.ordering, run_id=run_id,
            with_mpmath=req.with_mpmath)
    except SparseSpdError as exc:
        return JSONResponse(_error_body(exc, run_id))
    except Exception as exc:  # never report success on unknown failure
        log = RunLogger(run_id=run_id)
        log.error("unhandled_exception", error=type(exc).__name__,
                  message=str(exc))
        return JSONResponse(_error_body(
            SparseSpdError(str(exc)), run_id), status_code=500)

    return {
        "status": "ok",
        "run_id": run_id,
        "solution": [float(v) for v in result.x],
        "report": _report_out(result.report),
        "evidence": _evidence_out(result.evidence),
    }


@app.post("/api/v1/factor")
def factor(req: schemas.FactorRequest):
    run_id = _new_run_id()
    m = req.matrix
    try:
        _sin, _sym, fac, report, _log = default_engine.factorize(
            m.n, m.rows, m.cols, m.values,
            ordering=req.ordering, run_id=run_id)
    except SparseSpdError as exc:
        return JSONResponse(_error_body(exc, run_id))
    except Exception as exc:
        RunLogger(run_id=run_id).error(
            "unhandled_exception", error=type(exc).__name__,
            message=str(exc))
        return JSONResponse(_error_body(
            SparseSpdError(str(exc)), run_id), status_code=500)
    return {
        "status": "ok",
        "run_id": run_id,
        "diag_d": [float(v) for v in fac.diag],
        "report": _report_out(report),
    }


@app.post("/api/v1/orderings/compare")
def orderings_compare(req: schemas.OrderingCompareRequest):
    run_id = _new_run_id()
    m = req.matrix
    try:
        rows = default_engine.compare_orderings(
            m.n, m.rows, m.cols, m.values,
            methods=tuple(req.methods), run_id=run_id)
    except SparseSpdError as exc:
        return JSONResponse(_error_body(exc, run_id))
    return {"status": "ok", "run_id": run_id, "orderings": rows}


_FIXTURES = {
    "grid": lambda: fixtures.grid_laplacian(),
    "banded": lambda: fixtures.banded(),
    "arrowhead": lambda: fixtures.arrowhead(),
    "negative_diagonal": lambda: fixtures.negative_diagonal(),
    "indefinite": lambda: fixtures.indefinite_3x3(),
    "singular": lambda: fixtures.singular_matrix(),
}


@app.post("/api/v1/fixtures/run")
def fixtures_run(payload: dict):
    """Run a named synthetic fixture through the full pipeline."""
    run_id = _new_run_id()
    name = payload.get("name", "grid")
    ordering = payload.get("ordering", "minimum_degree")
    with_mp = bool(payload.get("with_mpmath", True))
    if name not in _FIXTURES:
        return JSONResponse(_error_body(
            SparseSpdError(
                f"unknown fixture {name!r}",
                details={"available": sorted(_FIXTURES)}),
            run_id), status_code=404)
    fx = _FIXTURES[name]()
    if not fx.spd:
        # Expected failure: assert the kernel classifies it correctly.
        try:
            default_engine.solve(
                fx.n, fx.rows, fx.cols, fx.vals, fx.rhs,
                ordering="natural", run_id=run_id,
                with_mpmath=False)
        except SparseSpdError as exc:
            body = _error_body(exc, run_id)
            body["expected_failure"] = True
            body["expected_pivot"] = fx.expected_bad_pivot
            return JSONResponse(body)
        return JSONResponse({
            "status": "error", "run_id": run_id,
            "error_code": "INTERNAL",
            "error_category": "test",
            "message": "fixture expected to fail but factorization "
                       "succeeded", "details": {}}, status_code=500)

    result = default_engine.solve(
        fx.n, fx.rows, fx.cols, fx.vals, fx.rhs,
        ordering=ordering, run_id=run_id, with_mpmath=with_mp)
    return {
        "status": "ok",
        "run_id": run_id,
        "fixture": name,
        "description": fx.description,
        "solution": [float(v) for v in result.x],
        "report": _report_out(result.report),
        "evidence": _evidence_out(result.evidence),
    }
