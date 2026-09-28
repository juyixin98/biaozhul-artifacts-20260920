"""Numerical input parsing and validation.

Boundary layer of the system: turns raw COO payloads into validated SciPy
sparse matrices and plain vectors.  Every rejection raises
:class:`InputValidationError` with a precise message; nothing downstream
re-validates.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import scipy.sparse as sp

from .errors import InputValidationError


@dataclass(frozen=True)
class ValidatedProblem:
    """A fully validated exp(t*A) @ v problem."""

    matrix: sp.csr_matrix
    vector: np.ndarray
    t: float
    tol: float


def _require_finite(name: str, values: np.ndarray) -> None:
    if not np.all(np.isfinite(values)):
        raise InputValidationError(f"{name} contains NaN or infinite values")


def build_matrix(n: int, row: list[int], col: list[int], data: list[float]) -> sp.csr_matrix:
    """Validate COO triplets and assemble a CSR matrix."""
    if not isinstance(n, int) or n < 1:
        raise InputValidationError(f"matrix dimension n must be a positive integer, got {n!r}")
    if not (len(row) == len(col) == len(data)):
        raise InputValidationError(
            f"COO arrays length mismatch: row={len(row)}, col={len(col)}, data={len(data)}"
        )
    rows = np.asarray(row, dtype=np.int64)
    cols = np.asarray(col, dtype=np.int64)
    vals = np.asarray(data, dtype=np.float64)
    # empty COO arrays are legal: they encode the zero matrix, for which
    # exp(t*A) @ v == v; the kernel handles it through the normal path.
    if rows.size > 0 and (rows.min() < 0 or cols.min() < 0 or rows.max() >= n or cols.max() >= n):
        raise InputValidationError(
            f"COO indices out of bounds for n={n}: "
            f"row range [{rows.min()}, {rows.max()}], col range [{cols.min()}, {cols.max()}]"
        )
    _require_finite("matrix data", vals)
    # sum duplicates deterministically via CSR canonicalisation
    return sp.coo_matrix((vals, (rows, cols)), shape=(n, n)).tocsr()


def build_vector(n: int, values: list[float]) -> np.ndarray:
    """Validate the right-hand-side vector."""
    vec = np.asarray(values, dtype=np.float64)
    if vec.ndim != 1 or vec.shape[0] != n:
        raise InputValidationError(
            f"vector length must equal matrix dimension n={n}, got shape {vec.shape}"
        )
    _require_finite("vector", vec)
    return vec


def build_scalar_time(t: float) -> float:
    """Validate the time horizon (zero and negative values are legal)."""
    t_f = float(t)
    if not np.isfinite(t_f):
        raise InputValidationError(f"t must be finite, got {t!r}")
    return t_f


def build_tolerance(tol: float | None, default: float) -> float:
    """Validate the requested tolerance against sane bounds."""
    if tol is None:
        return default
    tol_f = float(tol)
    if not np.isfinite(tol_f) or tol_f <= 0.0 or tol_f >= 1.0:
        raise InputValidationError(f"tol must be in (0, 1), got {tol!r}")
    return tol_f


def validate_problem(
    n: int,
    row: list[int],
    col: list[int],
    data: list[float],
    vector: list[float],
    t: float,
    tol: float | None,
    default_tol: float,
) -> ValidatedProblem:
    """Full validation pipeline for one request."""
    matrix = build_matrix(n, row, col, data)
    vec = build_vector(n, vector)
    return ValidatedProblem(
        matrix=matrix,
        vector=vec,
        t=build_scalar_time(t),
        tol=build_tolerance(tol, default_tol),
    )
