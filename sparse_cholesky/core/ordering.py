"""Fill-reducing orderings.

A permutation is applied **symmetrically** to a symmetric matrix:
``B = P A P^T``.  The right-hand side must follow the same permutation for
the reordered system ``B y = P b`` to be equivalent; the solution is mapped
back with ``x = P^T y``.  Getting only one side wrong silently destroys the
answer, so both transforms live here next to each other.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import sparse
from scipy.sparse.csgraph import reverse_cuthill_mckee

ORDERING_NATURAL = "natural"
ORDERING_RCM = "rcm"


@dataclass(frozen=True)
class Permutation:
    """New-to-old index mapping.

    ``perm[k]`` is the original index now occupying position ``k``.  Therefore
    ``B = A[perm, :][:, perm]`` and ``b_perm = b[perm]``.
    """

    perm: np.ndarray
    inv_perm: np.ndarray
    name: str

    @property
    def n(self) -> int:
        return int(self.perm.size)


def natural_ordering(n: int) -> Permutation:
    perm = np.arange(n, dtype=np.int64)
    return Permutation(perm=perm, inv_perm=perm.copy(), name=ORDERING_NATURAL)


def rcm_ordering(matrix_csc: sparse.csc_matrix) -> Permutation:
    """Reverse Cuthill-McKee ordering from the *structure* of the matrix."""
    n = matrix_csc.shape[0]
    # RCM only needs the sparsity pattern; values are irrelevant.
    structure = sparse.csr_matrix(
        (np.ones(matrix_csc.nnz, dtype=np.int8),
         matrix_csc.indices, matrix_csc.indptr),
        shape=matrix_csc.shape,
    )
    try:
        perm = reverse_cuthill_mckee(structure, symmetric_mode=True)
    except Exception as exc:  # pragma: no cover - scipy API guard
        raise RuntimeError(f"RCM ordering failed: {exc}") from exc
    perm = np.asarray(perm, dtype=np.int64)
    if perm.size != n or np.unique(perm).size != n:  # pragma: no cover
        raise RuntimeError("RCM returned an invalid permutation")
    inv_perm = np.empty(n, dtype=np.int64)
    inv_perm[perm] = np.arange(n, dtype=np.int64)
    return Permutation(perm=perm, inv_perm=inv_perm, name=ORDERING_RCM)


def compute_ordering(name: str, matrix_csc: sparse.csc_matrix) -> Permutation:
    if name == ORDERING_NATURAL:
        return natural_ordering(matrix_csc.shape[0])
    if name == ORDERING_RCM:
        return rcm_ordering(matrix_csc)
    raise ValueError(f"unknown ordering {name!r}; expected natural|rcm")


def permute_matrix(matrix_csc: sparse.csc_matrix,
                   permutation: Permutation) -> sparse.csc_matrix:
    """Return ``P A P^T`` without going through a dense array."""
    perm = permutation.perm
    reordered = matrix_csc[perm, :][:, perm].tocsc()
    reordered.sort_indices()
    return reordered


def permute_rhs(b: np.ndarray, permutation: Permutation) -> np.ndarray:
    """Permute the right-hand side: ``b_perm = P b``."""
    b = np.asarray(b, dtype=np.float64)
    if b.shape != (permutation.n,):
        raise ValueError(
            f"RHS has shape {b.shape}, expected ({permutation.n},)"
        )
    return b[permutation.perm]


def unpermute_solution(y: np.ndarray, permutation: Permutation) -> np.ndarray:
    """Map the reordered solution back: ``x = P^T y``."""
    y = np.asarray(y, dtype=np.float64)
    x = np.empty_like(y)
    x[permutation.perm] = y
    return x


def bandwidth(mat: sparse.spmatrix) -> int:
    """Maximum |i - j| over stored entries (semi-bandwidth of the pattern)."""
    coo = mat.tocoo()
    if coo.nnz == 0:
        return 0
    return int(np.max(np.abs(coo.row - coo.col)))
