"""Sparse matrix input handling.

Input arrives in COO form (rows / cols / values). It is validated and
stored as ``scipy.sparse.csr_matrix`` of the *upper* triangle. At no
point is the matrix densified: even symmetry checking and duplicate
detection work on sparse index structures.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import scipy.sparse as sp

from ..config import settings
from ..errors import ErrorCode, InputValidationError


@dataclass(frozen=True)
class SparseInput:
    """Validated symmetric matrix.

    Attributes:
        n: dimension.
        upper: CSR matrix containing exactly the upper triangle
            (i <= j), including the full diagonal.
        nnz_input: number of COO entries supplied by the caller.
        symmetric: whether the caller supplied both triangles with
            matching values (always True after successful construction).
    """

    n: int
    upper: sp.csr_matrix
    nnz_input: int
    symmetric: bool


def _check_indices(rows: np.ndarray, cols: np.ndarray, n: int) -> None:
    if np.any(rows < 0) or np.any(rows >= n) or np.any(cols < 0) \
            or np.any(cols >= n):
        bad = int(np.where((rows < 0) | (rows >= n) | (cols < 0)
                           | (cols >= n))[0][0])
        raise InputValidationError(
            ErrorCode.INVALID_INDICES,
            f"index out of range for {n}x{n} matrix at entry {bad}",
            details={"entry": bad, "n": n})


def from_coo(n: int, rows, cols, vals, *,
             check_symmetric: bool = True) -> SparseInput:
    """Build a validated sparse symmetric matrix from COO arrays.

    Both triangles may be supplied. Duplicate coordinates are rejected
    (no silent summation), so the input pattern is unambiguous.

    Raises:
        InputValidationError with a specific ErrorCode on any defect.
    """
    rows = np.asarray(rows, dtype=np.int64)
    cols = np.asarray(cols, dtype=np.int64)
    vals = np.asarray(vals, dtype=np.float64)

    if not isinstance(n, (int, np.integer)) or n <= 0:
        raise InputValidationError(
            ErrorCode.INVALID_SHAPE,
            f"dimension must be a positive integer, got {n!r}")
    if n > settings.max_dimension:
        raise InputValidationError(
            ErrorCode.TOO_LARGE,
            f"dimension {n} exceeds limit {settings.max_dimension}")
    if not (rows.shape == cols.shape == vals.shape) or rows.ndim != 1:
        raise InputValidationError(
            ErrorCode.INVALID_SHAPE,
            "rows, cols and vals must be 1-D arrays of equal length")
    if vals.size > settings.max_nnz_upper:
        raise InputValidationError(
            ErrorCode.TOO_LARGE,
            f"nnz {vals.size} exceeds limit {settings.max_nnz_upper}")
    if not np.all(np.isfinite(vals)):
        bad = int(np.where(~np.isfinite(vals))[0][0])
        raise InputValidationError(
            ErrorCode.INVALID_SHAPE,
            f"non-finite value at entry {bad}",
            details={"entry": bad})

    _check_indices(rows, cols, n)

    # Duplicate detection on linearized coordinates (sparse-only).
    lin = rows * n + cols
    if np.unique(lin).size != lin.size:
        u, c = np.unique(lin, return_counts=True)
        dup = int(u[c > 1][0])
        raise InputValidationError(
            ErrorCode.DUPLICATE_ENTRIES,
            "duplicate coordinate entries are not allowed",
            details={"row": int(dup // n), "col": int(dup % n)})

    # Every row needs a diagonal entry so that pivots are well defined.
    diag_lin = np.arange(n, dtype=np.int64) * (n + 1)
    if not np.all(np.isin(diag_lin, lin)):
        missing = int(np.where(~np.isin(diag_lin, lin))[0][0])
        raise InputValidationError(
            ErrorCode.INVALID_INDICES,
            f"missing diagonal entry at index {missing}",
            details={"index": missing})

    full = sp.coo_matrix((vals, (rows, cols)), shape=(n, n)).tocsr()

    if check_symmetric:
        _ensure_symmetric(full)

    upper_mat = sp.triu(full, format="csr")
    upper_mat.sort_indices()
    return SparseInput(n=n, upper=upper_mat, nnz_input=int(vals.size),
                       symmetric=True)


def _ensure_symmetric(a: sp.csr_matrix) -> None:
    """Check A == A^T in sparse structure and values, without densifying."""
    at = a.T.tocsr()
    a.sort_indices()
    at.sort_indices()
    # Structural difference.
    diff_struct = (a != 0).astype(np.int8) - (at != 0).astype(np.int8)
    if diff_struct.nnz != 0:
        coo = diff_struct.tocoo()
        i, j = int(coo.row[0]), int(coo.col[0])
        raise InputValidationError(
            ErrorCode.ASYMMETRIC,
            f"asymmetric sparsity pattern at ({i}, {j})",
            details={"row": i, "col": j})
    # Value difference on the common sparse pattern.
    delta = (a - at)
    if delta.nnz > 0:
        dvals = np.abs(delta.data)
        scale = max(np.max(np.abs(a.data)), 1.0)
        if np.max(dvals) > settings.symmetry_tol * scale:
            coo = delta.tocoo()
            k = int(np.argmax(np.abs(coo.data)))
            raise InputValidationError(
                ErrorCode.ASYMMETRIC,
                f"asymmetric values at ({int(coo.row[k])}, "
                f"{int(coo.col[k])})",
                details={"row": int(coo.row[k]),
                         "col": int(coo.col[k]),
                         "difference": float(coo.data[k])})


def from_dense(a: np.ndarray) -> SparseInput:
    """Testing/convenience helper: dense array -> SparseInput.

    Only used by tests and fixtures; the service path never densifies.
    """
    a = np.asarray(a, dtype=np.float64)
    if a.ndim != 2 or a.shape[0] != a.shape[1]:
        raise InputValidationError(ErrorCode.NOT_SQUARE,
                                   "matrix must be square")
    coo = sp.coo_matrix(a)
    return from_coo(a.shape[0], coo.row, coo.col, coo.data)


def to_upper_coo(upper: sp.csr_matrix):
    """Expose upper triangle back as (rows, cols, vals)."""
    c = upper.tocoo()
    return c.row.astype(np.int64), c.col.astype(np.int64), c.data.copy()
