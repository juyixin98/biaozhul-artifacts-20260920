"""FastAPI application exposing the sparse Cholesky backend.

Endpoints
---------
``GET  /health``                 liveness + dependency versions
``POST /api/v1/symbolic``        etree + fill report (no numeric work)
``POST /api/v1/factorize``       numeric factorization with pivot report
``POST /api/v1/solve``           factorize + solve + error evidence

Every response (including errors) carries the request's ``run_id`` so logs
can be correlated to the exact run.
"""
from __future__ import annotations

import numpy as np
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from config.settings import get_factorization_config, get_server_config
from sparse_cholesky.core import FactorizationEngine, SymbolicCache
from sparse_cholesky.input.errors import SparseCholeskyError
from sparse_cholesky.input.matrix import build_sparse_matrix

from .logging_setup import RunLogger, dependency_versions, new_run_id
from .schemas import (
    FillReport,
    MatrixInput,
    SolveRequest,
    SymbolicRequest,
)
from .service import (
    ServiceFailure,
    analyze_and_solve,
)


def create_app(log_dir: str | None = None,
               engine: FactorizationEngine | None = None) -> FastAPI:
    """Build the ASGI app.

    Parameters
    ----------
    log_dir:
        Override the JSONL run-log directory (tests use a temp dir).
    engine:
        Inject a preconfigured engine (tests use a tight-tolerance engine).
    """
    app = FastAPI(
        title="Sparse SPD Cholesky Backend",
        version="0.1.0",
        description="Symbolic and numeric sparse Cholesky (LDL^T) backend.",
    )
    server_cfg = get_server_config()
    run_logger = RunLogger(log_dir or server_cfg.log_dir, enabled=True)
    engine = engine or FactorizationEngine(get_factorization_config(),
                                           SymbolicCache())

    # Exposed for tests that want to inspect/prime the shared engine.
    app.state.run_logger = run_logger
    app.state.engine = engine

    @app.get("/health")
    def health() -> dict:
        return {
            "status": "ok",
            "versions": dependency_versions(),
            "config": {
                "pivot_tol_abs": engine.config.pivot_tol_abs,
                "max_order": engine.config.max_order,
            },
        }

    def _assemble(matrix_in: MatrixInput):
        return build_sparse_matrix(
            matrix_in.n,
            [e.row for e in matrix_in.entries],
            [e.col for e in matrix_in.entries],
            [e.value for e in matrix_in.entries],
        )

    def _failure_response(failure: ServiceFailure,
                          status_code: int = 422) -> JSONResponse:
        return JSONResponse(
            status_code=status_code,
            content={
                "success": False,
                "run_id": failure.run_id,
                "error_type": failure.error_type,
                "message": failure.message,
                "pivot_index": failure.pivot_index,
                "pivot_value": failure.pivot_value,
                "original_pivot_index": failure.original_pivot_index,
            },
        )

    @app.post("/api/v1/symbolic")
    def symbolic_only(req: SymbolicRequest) -> JSONResponse:
        run_id = req.run_id or new_run_id("sym")
        try:
            matrix = _assemble(req.matrix)
        except SparseCholeskyError as exc:
            run_logger.run_end(run_id, "failed", error_type=exc.error_type)
            return _failure_response(ServiceFailure(
                run_id=run_id, error_type=exc.error_type, message=str(exc)))

        result, _ = engine.factor(matrix, ordering=req.ordering)
        run_logger.run_end(run_id, "succeeded", stage="symbolic",
                           nnz_l=result.nnz_l)
        report = FillReport(
            nnz_a_lower=result.nnz_a_lower,
            nnz_l=result.nnz_l,
            fill_in=result.fill_in,
            fill_ratio=result.nnz_l / result.nnz_a_lower,
            etree_height=result.etree_height,
            bandwidth_before=result.bandwidth_before,
            bandwidth_after=result.bandwidth_after,
            ordering=result.ordering_name,
        )
        return JSONResponse(content={
            "success": True,
            "run_id": run_id,
            "n": matrix.n,
            "fill": report.model_dump(),
            "cache_hit": result.cache_hit,
        })

    @app.post("/api/v1/factorize")
    def factorize(req: SolveRequest) -> JSONResponse:
        return _run(req, with_solve=False)

    @app.post("/api/v1/solve")
    def solve(req: SolveRequest) -> JSONResponse:
        return _run(req, with_solve=True)

    def _run(req: SolveRequest, *, with_solve: bool) -> JSONResponse:
        run_id = req.run_id or new_run_id("solve" if with_solve else "fac")
        try:
            matrix = _assemble(req.matrix)
        except SparseCholeskyError as exc:
            run_logger.event(run_id, "input", "failed",
                             error_type=exc.error_type)
            run_logger.run_end(run_id, "failed", error_type=exc.error_type)
            return _failure_response(ServiceFailure(
                run_id=run_id, error_type=exc.error_type, message=str(exc)))

        rhs = None
        if with_solve:
            if req.rhs is None:
                return _failure_response(ServiceFailure(
                    run_id=run_id,
                    error_type="input_validation_error",
                    message="'rhs' is required for /api/v1/solve"))
            if len(req.rhs) != matrix.n:
                return _failure_response(ServiceFailure(
                    run_id=run_id,
                    error_type="matrix_shape_error",
                    message=(f"rhs length {len(req.rhs)} != matrix order "
                             f"{matrix.n}")))
            rhs = np.asarray(req.rhs, dtype=np.float64)

        outcome = analyze_and_solve(
            matrix,
            ordering=req.ordering,
            rhs=rhs,
            run_id=run_id,
            engine=engine,
            logger=run_logger,
        )
        if isinstance(outcome, ServiceFailure):
            return _failure_response(outcome)
        return JSONResponse(content={
            "success": True,
            "run_id": outcome.run_id,
            "n": outcome.n,
            "ordering": outcome.ordering,
            "fill": outcome.fill,
            "pivots": outcome.pivots,
            "evidence": outcome.evidence,
            "cache_hit": outcome.cache_hit,
            **({"solution": outcome.solution} if with_solve else {}),
        })

    return app


app = create_app()
