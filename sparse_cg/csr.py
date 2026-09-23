"""Compressed Sparse Row (CSR) matrix with strict validation.

The class is deliberately minimal: it implements exactly the operations the
PCG solver needs (matrix-vector product, diagonal, symmetry/definiteness
checks) and validates its inputs aggressively so malformed CSR structures
are rejected at construction time rather than causing garbage results later.

Construction either supplies raw CSR arrays directly::

    CSRMatrix(n, indptr, indices, data)

or builds from COO (triplet) entries, summing duplicates::

    CSRMatrix.from_triplets(n, rows, cols, vals)

Duplicate entries supplied to either constructor are summed; explicit
zero-result entries are then dropped.  The stored arrays are always in
canonical form: each row's column indices strictly increasing.
"""

from __future__ import annotations

from typing import Iterable

import numpy as np

from .exceptions import (
    InvalidCsrError,
    NonSymmetricMatrixError,
    NotPositiveDefiniteError,
)
from .limits import MAX_ABS_VALUE, MAX_N, MAX_NNZ


def _is_integer_array(a: np.ndarray) -> bool:
    """True only for genuine integer arrays (NumPy int dtypes, no bools)."""
    return np.issubdtype(a.dtype, np.integer) and not np.issubdtype(
        a.dtype, np.bool_
    )


class CSRMatrix:
    """Square sparse matrix in CSR form.

    Parameters
    ----------
    n:
        Matrix dimension (n x n).
    indptr:
        Row pointer array, length ``n + 1``.  ``indptr[0]`` must be 0,
        non-decreasing, and ``indptr[-1] == len(data) == len(indices)``.
    indices:
        Column index of each stored entry, values in ``[0, n)``.
    data:
        Numeric value of each stored entry.

    Raises
    ------
    InvalidCsrError
        Structural or range validation failure (message is specific).
    """

    def __init__(self, n: int, indptr: Iterable, indices: Iterable,
                 data: Iterable) -> None:
        # ---- n ----------------------------------------------------------------
        if isinstance(n, bool) or not isinstance(n, (int, np.integer)):
            raise InvalidCsrError(f"'n' must be an integer, got {type(n).__name__}")
        n = int(n)
        if n <= 0:
            raise InvalidCsrError(f"'n' must be positive, got {n}")
        if n > MAX_N:
            raise InvalidCsrError(
                f"n={n} exceeds the small-to-medium limit MAX_N={MAX_N}"
            )
        self.n = n

        # ---- array conversion / dtype ----------------------------------------
        try:
            indptr = np.asarray(indptr)
            indices = np.asarray(indices)
            data = np.asarray(data, dtype=np.float64)
        except (TypeError, ValueError) as exc:
            raise InvalidCsrError(f"CSR arrays are not numeric: {exc}") from exc

        if not _is_integer_array(indptr):
            raise InvalidCsrError(
                f"'indptr' must be an integer array, got dtype {indptr.dtype}"
            )
        if not _is_integer_array(indices):
            raise InvalidCsrError(
                f"'indices' must be an integer array, got dtype {indices.dtype}"
            )
        if data.ndim != 1 or indices.ndim != 1 or indptr.ndim != 1:
            raise InvalidCsrError("'indptr', 'indices' and 'data' must be 1-D arrays")

        if indptr.shape != (n + 1,):
            raise InvalidCsrError(
                f"'indptr' must have length n+1={n + 1}, got {len(indptr)}"
            )
        if indices.shape != data.shape:
            raise InvalidCsrError(
                f"'indices' and 'data' must have equal lengths, "
                f"got {len(indices)} and {len(data)}"
            )

        nnz = int(indices.size)
        if nnz > MAX_NNZ:
            raise InvalidCsrError(
                f"nnz={nnz} exceeds the small-to-medium limit MAX_NNZ={MAX_NNZ}"
            )

        # ---- indptr structure --------------------------------------------------
        if int(indptr[0]) != 0:
            raise InvalidCsrError(f"'indptr[0]' must be 0, got {int(indptr[0])}")
        if int(indptr[-1]) != nnz:
            raise InvalidCsrError(
                f"'indptr[-1]={int(indptr[-1])}' must equal nnz={nnz}"
            )
        diffs = np.diff(indptr.astype(np.int64))
        if np.any(diffs < 0):
            bad = int(np.argmin(diffs))
            raise InvalidCsrError(
                f"'indptr' must be non-decreasing; decrease at row {bad} "
                f"(indptr[{bad}]={int(indptr[bad])}, "
                f"indptr[{bad + 1}]={int(indptr[bad + 1])})"
            )
        if np.any(indptr < 0):
            raise InvalidCsrError("'indptr' must contain non-negative values")

        # ---- column indices ----------------------------------------------------
        if int(indices.size) and int(indices.min()) < 0:
            raise InvalidCsrError("'indices' contains a negative column index")
        if int(indices.size) and int(indices.max()) >= n:
            raise InvalidCsrError(
                f"'indices' contains column index >= n={n}"
            )

        # ---- values ------------------------------------------------------------
        if not np.all(np.isfinite(data)):
            bad = int(np.argmax(~np.isfinite(data)))
            raise InvalidCsrError(
                f"'data' contains non-finite value at position {bad}: {data[bad]}"
            )
        if data.size and float(np.max(np.abs(data))) > MAX_ABS_VALUE:
            bad = int(np.argmax(np.abs(data)))
            raise InvalidCsrError(
                f"|data[{bad}]|={abs(float(data[bad])):.3e} exceeds "
                f"MAX_ABS_VALUE={MAX_ABS_VALUE:.0e}"
            )

        # ---- canonicalize: per-row sort, duplicate sum, drop zeros ------------
        self.indptr, self.indices, self.data = self._canonicalize(
            indptr.astype(np.intp),
            indices.astype(np.intp),
            data,
        )
        self.nnz = int(self.indices.size)

    # ------------------------------------------------------------------ #
    # Construction helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _canonicalize(indptr: np.ndarray, indices: np.ndarray,
                      data: np.ndarray) -> tuple[np.ndarray, np.ndarray,
                                                 np.ndarray]:
        """Sort each row by column, sum duplicates, drop explicit zeros."""
        n = len(indptr) - 1
        out_indptr = np.zeros(n + 1, dtype=np.intp)
        out_indices_parts: list[np.ndarray] = []
        out_data_parts: list[np.ndarray] = []

        for i in range(n):
            s, e = int(indptr[i]), int(indptr[i + 1])
            if e == s:
                out_indptr[i + 1] = out_indptr[i]
                continue
            col = indices[s:e]
            val = data[s:e]
            order = np.argsort(col, kind="stable")
            col = col[order]
            val = val[order]

            # Detect boundaries of equal column indices and sum the groups.
            same = np.concatenate(([False], col[1:] == col[:-1]))
            group_starts = np.flatnonzero(~same)
            group_ends = np.concatenate((group_starts[1:], [len(col)]))
            sums = np.add.reduceat(val, group_starts)
            col_uniq = col[group_starts]

            keep = sums != 0.0
            col_uniq = col_uniq[keep]
            sums = sums[keep]

            out_indices_parts.append(col_uniq)
            out_data_parts.append(sums)
            out_indptr[i + 1] = out_indptr[i] + col_uniq.size

        out_indices = (np.concatenate(out_indices_parts)
                       if out_indices_parts else np.zeros(0, dtype=np.intp))
        out_data = (np.concatenate(out_data_parts)
                    if out_data_parts else np.zeros(0, dtype=np.float64))
        return out_indptr, out_indices, out_data

    @classmethod
    def from_triplets(cls, n: int, rows: Iterable, cols: Iterable,
                      values: Iterable) -> "CSRMatrix":
        """Build a CSR matrix from COO triplets (duplicates are summed).

        Each k-th entry is ``A[rows[k], cols[k]] += values[k]``.
        """
        try:
            rows = np.asarray(rows)
            cols = np.asarray(cols)
            values = np.asarray(values, dtype=np.float64)
        except (TypeError, ValueError) as exc:
            raise InvalidCsrError(f"triplet arrays are not numeric: {exc}") from exc

        if not _is_integer_array(rows) or not _is_integer_array(cols):
            raise InvalidCsrError(
                "'rows' and 'cols' must both be integer arrays"
            )
        if rows.ndim != 1 or cols.ndim != 1 or values.ndim != 1:
            raise InvalidCsrError("triplet arrays must all be 1-D")
        if not (rows.shape == cols.shape == values.shape):
            raise InvalidCsrError(
                f"triplet arrays must have equal lengths, got "
                f"{rows.shape[0]}, {cols.shape[0]}, {values.shape[0]}"
            )
        if values.size > MAX_NNZ:
            raise InvalidCsrError(
                f"nnz={values.size} exceeds MAX_NNZ={MAX_NNZ}"
            )

        # Light early range check so that bin-counting cannot run on garbage.
        if values.size:
            if int(rows.min()) < 0 or int(cols.min()) < 0:
                raise InvalidCsrError("triplet rows/cols contain a negative index")
            if int(rows.max()) >= n or int(cols.max()) >= n:
                raise InvalidCsrError(
                    f"triplet rows/cols contain an index >= n={n}"
                )

        counts = np.bincount(rows.astype(np.intp), minlength=n)
        indptr = np.zeros(n + 1, dtype=np.intp)
        np.cumsum(counts, out=indptr[1:])
        order = np.argsort(rows.astype(np.intp), kind="stable")
        return cls(n, indptr, cols[order], values[order])

    # ------------------------------------------------------------------ #
    # Core sparse operations
    # ------------------------------------------------------------------ #
    def matvec(self, x: np.ndarray) -> np.ndarray:
        """Compute ``y = A @ x`` for a length-n vector ``x``."""
        x = np.asarray(x, dtype=np.float64)
        if x.shape != (self.n,):
            raise ValueError(
                f"x must have shape ({self.n},), got {x.shape}"
            )
        y = np.zeros(self.n, dtype=np.float64)
        # np.add.reduceat over each row is one vectorized call per row;
        # fine for n <= 10_000, and transparent to read/audit.
        for i in range(self.n):
            s, e = self.indptr[i], self.indptr[i + 1]
            if e > s:
                y[i] = np.dot(self.data[s:e], x[self.indices[s:e]])
        return y

    def diagonal(self) -> np.ndarray:
        """Return the diagonal of A (0 for rows with no stored diagonal)."""
        diag = np.zeros(self.n, dtype=np.float64)
        for i in range(self.n):
            s, e = self.indptr[i], self.indptr[i + 1]
            loc = np.searchsorted(self.indices[s:e], i)
            if s + loc < e and self.indices[s + loc] == i:
                diag[i] = self.data[s + loc]
        return diag

    # ------------------------------------------------------------------ #
    # SPD-related checks
    # ------------------------------------------------------------------ #
    def assert_symmetric(self, rtol: float = 1e-10,
                         atol: float = 1e-12) -> None:
        """Raise NonSymmetricMatrixError unless A == A^T.

        Both the sparsity *pattern* and the values must match: for every
        stored (i, j) there must be a stored (j, i), and the value pair must
        agree within ``atol + rtol * |A_ij|``.

        Returns the worst observed absolute difference when symmetric
        (useful for diagnostics).
        """
        if not (0.0 < rtol <= 1.0):
            raise ValueError("rtol must lie in (0, 1]")
        if atol < 0.0:
            raise ValueError("atol must be >= 0")

        max_abs_diff = 0.0
        for i in range(self.n):
            s, e = self.indptr[i], self.indptr[i + 1]
            cols_i = self.indices[s:e]
            vals_i = self.data[s:e]

            # Column indices in the mirrored row j = cols_i, entry (j, i).
            for k in range(e - s):
                j = int(cols_i[k])
                ts, te = self.indptr[j], self.indptr[j + 1]
                loc = np.searchsorted(self.indices[ts:te], i)
                if ts + loc >= te or self.indices[ts + loc] != i:
                    raise NonSymmetricMatrixError(
                        f"sparsity pattern not symmetric: entry "
                        f"A[{i},{j}]={float(vals_i[k]):.6g} has no "
                        f"A[{j},{i}] counterpart",
                        max_abs_diff=-1.0,
                        position=(i, j),
                    )
                a_ji = float(self.data[ts + loc])
                diff = abs(float(vals_i[k]) - a_ji)
                if diff > max_abs_diff:
                    max_abs_diff = diff
                tol = atol + rtol * abs(float(vals_i[k]))
                if diff > tol:
                    raise NonSymmetricMatrixError(
                        f"matrix not symmetric: |A[{i},{j}] - A[{j},{i}]| "
                        f"= {diff:.6e} exceeds tolerance {tol:.6e}",
                        max_abs_diff=diff,
                        position=(i, j),
                    )

    def assert_positive_diagonal(self) -> None:
        """Raise unless every diagonal entry is strictly positive.

        A symmetric matrix with a non-positive diagonal cannot be SPD
        (x = e_i gives x^T A x = A_ii <= 0).  This catches semidefinite /
        indefinite matrices with a zero or negative diagonal before wasting
        iterations; indefinite matrices with a positive diagonal surface
        later as non-positive curvature inside CG.
        """
        diag = self.diagonal()
        for i, d in enumerate(diag):
            if not np.isfinite(d) or d <= 0.0:
                raise NotPositiveDefiniteError(
                    f"diagonal entry A[{i},{i}]={d:.6g} is not strictly "
                    f"positive; the matrix cannot be symmetric positive "
                    f"definite",
                    index=i,
                    diagonal_value=float(d),
                )

    # ------------------------------------------------------------------ #
    # Misc
    # ------------------------------------------------------------------ #
    def to_dense(self) -> np.ndarray:
        """Return a dense n x n copy (for tests/debugging only)."""
        a = np.zeros((self.n, self.n), dtype=np.float64)
        for i in range(self.n):
            s, e = self.indptr[i], self.indptr[i + 1]
            a[i, self.indices[s:e]] = self.data[s:e]
        return a

    def __repr__(self) -> str:
        return f"CSRMatrix(n={self.n}, nnz={self.nnz})"
