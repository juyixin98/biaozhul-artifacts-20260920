"""Service layer: one orchestration call per API/demo operation.

This layer is independent of FastAPI so it can be unit-tested directly.  It
maps typed core errors to categorized failure records and translates a
permuted failing pivot back to the user's original index.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from config.settings import get_factorization_config
from sparse_cholesky.core import (
    FactorizationEngine,
    permute_matrix,
)
from sparse_cholesky.evidence import (
    reconstruction_evidence,
    residual_evidence,
)
from sparse_cholesky.input.errors import (
    FactorizationError,
    InputValidationError,
    SymbolicStructureMismatchError,
)
from sparse_cholesky.input.matrix import SparseMatrix, build_sparse_matrix

from .logging_setup import RunLogger, new_run_id


@dataclass
class ServiceFailure:
    success: bool = False
    run_id: str = ""
    error_type: str = "unknown_error"
    message: str = ""
    pivot_index: int | None = None
    pivot_value: float | None = None
    original_pivot_index: int | None = None


@dataclass
class ServiceSuccess:
    success: bool = True
    run_id: str = ""
    n: int = 0
    ordering: str = ""
    fill: dict[str, Any] = field(default_factory=dict)
    pivots: dict[str, float] = field(default_factory=dict)
    solution: list[float] | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    cache_hit: bool = False


def build_matrix_from_entries(
    n: int, entries: list[tuple[int, int, float]]
) -> SparseMatrix:
    rows = [e[0] for e in entries]
    cols = [e[1] for e in entries]
    vals = [e[2] for e in entries]
    return build_sparse_matrix(n, rows, cols, vals)


def analyze_and_solve(
    matrix: SparseMatrix,
    *,
    ordering: str = "natural",
    rhs: np.ndarray | None = None,
    run_id: str | None = None,
    engine: FactorizationEngine | None = None,
    logger: RunLogger | None = None,
    include_solution: bool = True,
) -> ServiceSuccess | ServiceFailure:
    """Run ordering -> symbolic -> numeric -> solve -> evidence with logging.

    Expected :class:`SparseCholeskyError` categories are converted into a
    :class:`ServiceFailure` carrying the concrete category; they are never
    reported as success.
    """
    run_id = run_id or new_run_id()
    logger = logger if logger is not None else RunLogger("", enabled=False)
    engine = engine or FactorizationEngine(get_factorization_config())

    logger.run_start(run_id, {"n": matrix.n, "ordering": ordering,
                              "nnz_input": matrix.nnz_input,
                              "has_rhs": rhs is not None})

    def log(step: str, status: str, **payload: Any) -> None:
        logger.event(run_id, step, status, **payload)

    try:
        result, x = engine.factor(
            matrix,
            ordering=ordering,
            b=rhs,
            progress=lambda step, payload: log(step, "progress", **payload),
        )
    except FactorizationError as exc:
        log("numeric", "failed", error_type=exc.error_type,
            pivot_index=exc.pivot_index,
            original_pivot_index=exc.ordering_index,
            pivot_value=exc.pivot_value)
        logger.run_end(run_id, "failed", error_type=exc.error_type)
        return ServiceFailure(
            run_id=run_id,
            error_type=exc.error_type,
            message=str(exc),
            pivot_index=exc.pivot_index,
            pivot_value=exc.pivot_value,
            original_pivot_index=exc.ordering_index,
        )
    except (InputValidationError, SymbolicStructureMismatchError) as exc:
        log("input", "failed", error_type=exc.error_type, message=str(exc))
        logger.run_end(run_id, "failed", error_type=exc.error_type)
        return ServiceFailure(
            run_id=run_id, error_type=exc.error_type, message=str(exc)
        )

    # Reconstruction is checked in the permuted frame (that is where the
    # numeric factor lives); the residual is checked in original coordinates.
    permuted_csc = permute_matrix(matrix.csc, result.permutation)
    rec = reconstruction_evidence(permuted_csc, result.numeric)
    evidence: dict[str, Any] = {
        "reconstruction": {
            "relative_fro": rec.relative_fro,
            "max_abs_diff": rec.max_abs_diff,
            "factor_pattern_mismatches": rec.factor_pattern_mismatches,
            "max_cancellation": rec.max_cancellation,
            "criterion": "||LDL^T - A||_F / ||A||_F; L pattern must equal "
                         "the symbolic prediction; fill cancellations at A's "
                         "structural zeros must be tiny (all sparse)",
        }
    }
    log("evidence:reconstruction", "ok",
        relative_fro=rec.relative_fro,
        factor_pattern_mismatches=rec.factor_pattern_mismatches,
        max_cancellation=rec.max_cancellation)

    solution_out: list[float] | None = None
    if x is not None:
        res = residual_evidence(matrix.csc, x, rhs)
        evidence["residual"] = {
            "relative_residual_inf": res.relative_residual_inf,
            "relative_residual_2": res.relative_residual_2,
            "max_residual_component": res.max_residual_component,
            "nnz_residual": res.nnz_residual,
            "criterion": "||b-Ax|| / ||b|| using sparse A@x (no densification)",
        }
        log("evidence:residual", "ok",
            relative_residual_inf=res.relative_residual_inf,
            relative_residual_2=res.relative_residual_2,
            max_residual_component=res.max_residual_component)
        if include_solution:
            solution_out = [float(v) for v in x]

    logger.run_end(run_id, "succeeded", nnz_l=result.nnz_l,
                   fill_in=result.fill_in)
    return ServiceSuccess(
        run_id=run_id,
        n=matrix.n,
        ordering=result.ordering_name,
        fill={
            "nnz_a_lower": result.nnz_a_lower,
            "nnz_l": result.nnz_l,
            "fill_in": result.fill_in,
            "fill_ratio": result.nnz_l / result.nnz_a_lower,
            "etree_height": result.etree_height,
            "bandwidth_before": result.bandwidth_before,
            "bandwidth_after": result.bandwidth_after,
        },
        pivots={
            "min": float(np.min(result.pivots)),
            "max": float(np.max(result.pivots)),
        },
        solution=solution_out,
        evidence=evidence,
        cache_hit=result.cache_hit,
    )
