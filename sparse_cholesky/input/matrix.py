"""Sparse symmetric matrix container.

The input contract for the whole backend is COO triples for the *lower*
triangle of a symmetric matrix.  Internally we keep a CSR representation for
fast row access during the numeric factorization; the matrix is never fully
densified.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import sparse

from .errors import (
    DuplicateEntryError,
    MatrixNotFiniteError,
    MatrixShapeError,
    OffDiagonalLowerError,
)

#: Floating point type used across the numeric kernels.
FLOAT_DTYPE = np.float64


@dataclass(frozen=True)
class SparseMatrix:
    """An immutable view of a sparse symmetric matrix.

    Attributes
    ----------
    n:
        Matrix order.
    csc:
        The symmetric matrix assembled in CSC form.
    lower_csr:
        Lower triangle (``i >= j``) in CSR, the form consumed by the kernels.
    nnz_input:
        Number of user supplied entries (lower triangle only).
    """

    n: int
    csc: sparse.csc_matrix
    lower_csr: sparse.csr_matrix
    nnz_input: int

    @property
    def nnz_symmetric(self) -> int:
        """Number of stored entries in the full symmetric representation."""
        return int(self.csc.nnz)

    def density(self) -> float:
        total = self.n * self.n
        if total == 0:
            return 0.0
        return self.csc.nnz / total


def build_sparse_matrix(
    n: int,
    rows: np.ndarray | list[int],
    cols: np.ndarray | list[int],
    vals: np.ndarray | list[float],
    *,
    check_finite: bool = True,
) -> SparseMatrix:
    """Validate raw COO triples and build a :class:`SparseMatrix`.

    Only entries on or below the diagonal (``row >= col``) are accepted; the
    upper triangle is the mirror image.  Explicit zero entries are rejected so
    that the sparsity pattern reflects the user's intent.

    Raises
    ------
    MatrixShapeError
        Wrong order, mismatched array lengths, or out of range indices.
    OffDiagonalLowerError
        An entry above the diagonal was supplied.
    DuplicateEntryError
        The same (row, col) pair occurs more than once.
    MatrixNotFiniteError
        A value is NaN or infinite.
    MatrixNotSymmetricError
        Internal sanity check failure (should not normally happen).
    """
    rows = np.asarray(rows, dtype=np.int64)
    cols = np.asarray(cols, dtype=np.int64)
    vals = np.asarray(vals, dtype=FLOAT_DTYPE)

    if not isinstance(n, (int, np.integer)) or n <= 0:
        raise MatrixShapeError(f"matrix order must be a positive integer, got {n!r}")
    n = int(n)

    if rows.shape != cols.shape or rows.shape != vals.shape or rows.ndim != 1:
        raise MatrixShapeError(
            "rows, cols and vals must be 1-D arrays of equal length, "
            f"got shapes {rows.shape}, {cols.shape}, {vals.shape}"
        )

    if rows.size > 0:
        if int(rows.min()) < 0 or int(cols.min()) < 0:
            raise MatrixShapeError("row/column indices must be non-negative")
        if int(rows.max()) >= n or int(cols.max()) >= n:
            raise MatrixShapeError(
                f"row/column index exceeds matrix order n={n}"
            )
        if np.any(rows < cols):
            bad = int(np.flatnonzero(rows < cols)[0])
            raise OffDiagonalLowerError(
                f"only lower-triangular entries (row >= col) are accepted; "
                f"entry #{bad} is ({int(rows[bad])}, {int(cols[bad])})"
            )

    if check_finite and vals.size > 0 and not np.all(np.isfinite(vals)):
        bad = int(np.flatnonzero(~np.isfinite(vals))[0])
        raise MatrixNotFiniteError(
            f"entry ({int(rows[bad])}, {int(cols[bad])}) is not finite: {vals[bad]}"
        )

    # An explicit zero *off-diagonal* is meaningless in sparse storage; a
    # zero *diagonal* is retained, because it must reach the numeric kernel
    # and be reported there as a localized zero pivot (non-PD), not rejected
    # as malformed input.
    off_diag_mask = rows != cols
    if np.any(off_diag_mask & (vals == 0.0)):
        bad = int(np.flatnonzero(off_diag_mask & (vals == 0.0))[0])
        raise MatrixShapeError(
            f"explicit zero off-diagonal entries are not allowed; remove "
            f"({int(rows[bad])}, {int(cols[bad])}) from the input"
        )

    # Duplicate (row, col) detection without densifying.
    if rows.size > 1:
        order = np.lexsort((cols, rows))
        sr, sc = rows[order], cols[order]
        same = (sr[1:] == sr[:-1]) & (sc[1:] == sc[:-1])
        if np.any(same):
            idx = int(np.flatnonzero(same)[0])
            raise DuplicateEntryError(
                f"duplicate entry at ({int(sr[idx + 1])}, {int(sc[idx + 1])})"
            )

    # Assemble the symmetric matrix from the lower triangle.  Explicit zeros
    # (only possible on the diagonal) must survive, so do not eliminate them.
    lower = sparse.coo_matrix((vals, (rows, cols)), shape=(n, n)).tocsr()
    lower.sum_duplicates()  # defensive: duplicates already rejected above
    symmetric = lower + lower.T
    # Adding the transpose doubles the diagonal; restore the original diagonal.
    diag = np.asarray(lower.diagonal())
    symmetric.setdiag(diag)
    symmetric = symmetric.tocsc()
    symmetric.sort_indices()
    # Drop explicit zero entries from storage (a present-but-zero diagonal
    # was already accepted structurally; the numeric kernel reads a missing
    # entry as zero and reports the localized zero pivot anyway).
    symmetric.eliminate_zeros()

    # Every row/column of an SPD matrix carries a diagonal *slot*.  Presence
    # is structural (checked from coordinates), because a present-but-zero
    # diagonal must survive to the numeric kernel as a localized zero pivot.
    diag_slots = np.zeros(n, dtype=bool)
    diag_slots[rows[rows == cols]] = True
    if not np.all(diag_slots):
        missing = int(np.flatnonzero(~diag_slots)[0])
        raise MatrixShapeError(
            f"row {missing} has no diagonal entry; a symmetric positive "
            "definite matrix requires a diagonal slot for every index"
        )

    # Structural symmetry is guaranteed by construction (lower + its
    # transpose), so no dense comparison is performed here; the sparse
    # representation is the only materialized form.
    return SparseMatrix(
        n=n,
        csc=symmetric,
        lower_csr=lower,
        nnz_input=int(rows.size),
    )
