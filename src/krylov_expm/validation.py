"""Numerical input validation and normalization (COO -> CSR, edge cases)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import scipy.sparse as sp

from .config import SolverConfig
from .errors import ExpmvFailure, FailureCategory
from .models import ExpmvRequest


@dataclass
class NormalizedInput:
    matrix: sp.csr_matrix
    vector: np.ndarray
    t: float
    tol: float
    trivial_case: Optional[str]  # "zero_time" | "zero_vector" | None


def _fail(message: str) -> ExpmvFailure:
    return ExpmvFailure(FailureCategory.VALIDATION_ERROR, message)


def _validate_matrix_payload(req: ExpmvRequest) -> None:
    m = req.matrix
    if len(m.shape) != 2 or m.shape[0] != m.shape[1]:
        raise _fail(f"matrix must be square, got shape {m.shape}")
    n = m.shape[0]
    if n < 1:
        raise _fail("matrix dimension must be >= 1")
    if not (len(m.row) == len(m.col) == len(m.data)):
        raise _fail(
            f"COO length mismatch: row={len(m.row)} col={len(m.col)} data={len(m.data)}"
        )
    if m.row and (min(m.row) < 0 or max(m.row) >= n or min(m.col) < 0 or max(m.col) >= n):
        raise _fail("COO indices out of range")
    if not np.all(np.isfinite(np.asarray(m.data, dtype=np.float64))):
        raise _fail("matrix data contains NaN or Inf")


def validate_and_normalize(req: ExpmvRequest, config: SolverConfig) -> NormalizedInput:
    """Validate a request and normalize it to CSR + float64 vector.

    Edge cases are explicit: t == 0 and the zero vector are flagged as
    trivial cases (handled without Krylov iteration); negative t is a
    valid backward propagation and passes through unchanged.
    """
    _validate_matrix_payload(req)
    n = req.matrix.shape[0]
    vector = np.asarray(req.vector, dtype=np.float64)
    if vector.shape != (n,):
        raise _fail(f"vector length {vector.size} != matrix dimension {n}")
    if not np.all(np.isfinite(vector)):
        raise _fail("vector contains NaN or Inf")
    if not np.isfinite(req.t):
        raise _fail("t must be finite")
    tol = req.tol if req.tol is not None else config.default_tol
    if not (0.0 < tol < 1.0):
        raise _fail(f"tol must be in (0, 1), got {tol}")
    matrix = sp.coo_matrix(
        (
            np.asarray(req.matrix.data, dtype=np.float64),
            (np.asarray(req.matrix.row), np.asarray(req.matrix.col)),
        ),
        shape=(n, n),
    ).tocsr()
    trivial_case = None
    if req.t == 0.0:
        trivial_case = "zero_time"
    elif not np.any(vector):
        trivial_case = "zero_vector"
    return NormalizedInput(
        matrix=matrix, vector=vector, t=float(req.t), tol=tol, trivial_case=trivial_case
    )
