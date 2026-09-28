"""Error evidence computed *without* densifying the matrix.

The evidence layer answers "is the factorization actually correct?" using
only sparse operations:

* sparse residual          ``r = b - A x``
* sparse reconstruction    ``L D L^T - A``
* componentwise residuals against a dense reference (only in tests on small
  matrices)
* relative forward error against a high-precision mpmath solution

All norms are computed from sparse structures, so gathering evidence costs
``O(nnz)`` rather than ``O(n^2)``.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import sparse

from sparse_cholesky.core.numeric import NumericFactor


@dataclass(frozen=True)
class ResidualEvidence:
    residual_norm_inf: float
    rhs_norm_inf: float
    relative_residual_inf: float
    residual_norm_2: float
    rhs_norm_2: float
    relative_residual_2: float
    max_residual_component: int
    nnz_residual: int


def sparse_residual(matrix_csc: sparse.csc_matrix, x: np.ndarray,
                    b: np.ndarray, *, tol: float = 0.0) -> sparse.csc_matrix:
    """Return the sparse residual vector ``r = b - A x`` as a 1-column CSC."""
    r = b - matrix_csc @ x
    if tol > 0.0:
        r[np.abs(r) < tol] = 0.0
    return sparse.csc_matrix(r.reshape(-1, 1))


def residual_evidence(matrix_csc: sparse.csc_matrix, x: np.ndarray,
                      b: np.ndarray) -> ResidualEvidence:
    r = b - matrix_csc @ x
    rnorm_inf = float(np.linalg.norm(r, ord=np.inf))
    bnorm_inf = float(np.linalg.norm(b, ord=np.inf))
    rnorm_2 = float(np.linalg.norm(r, ord=2))
    bnorm_2 = float(np.linalg.norm(b, ord=2))
    rel_inf = rnorm_inf / bnorm_inf if bnorm_inf > 0 else float(rnorm_inf)
    rel_2 = rnorm_2 / bnorm_2 if bnorm_2 > 0 else float(rnorm_2)
    return ResidualEvidence(
        residual_norm_inf=rnorm_inf,
        rhs_norm_inf=bnorm_inf,
        relative_residual_inf=rel_inf,
        residual_norm_2=rnorm_2,
        rhs_norm_2=bnorm_2,
        relative_residual_2=rel_2,
        max_residual_component=int(np.argmax(np.abs(r))),
        nnz_residual=int(np.count_nonzero(r)),
    )


def reconstruct_factor(factor: NumericFactor) -> sparse.csc_matrix:
    """Reconstruct ``L D L^T`` sparsely from the numeric factor."""
    l_mat = factor.l_csc()
    d_mat = sparse.diags(factor.diag, format="csc")
    return ((l_mat @ d_mat) @ l_mat.T).tocsc()


@dataclass(frozen=True)
class ReconstructionEvidence:
    diff_norm_fro: float
    a_norm_fro: float
    relative_fro: float
    max_abs_diff: float
    #: Positions where the stored L pattern disagrees with the symbolic
    #: prediction (must be 0 for a structurally consistent factorization).
    factor_pattern_mismatches: int
    #: Entries of L D L^T at positions that are structural zeros of A.
    #: Fill in L cancels numerically at these positions; their magnitude
    #: measures how cleanly the sparsity of A is recovered.
    cancellation_positions: int
    max_cancellation: float


def reconstruction_evidence(matrix_csc: sparse.csc_matrix,
                            factor: NumericFactor) -> ReconstructionEvidence:
    """Compare ``A`` with the sparsely reconstructed ``L D L^T``.

    Two distinct questions are answered:

    * **Factor structure**: does the stored pattern of ``L`` exactly match
      the symbolic prediction?  (``factor_pattern_mismatches`` must be 0.)
    * **Numerical recovery of A**: the product ``L D L^T`` carries explicit
      entries at every fill position, including ones that cancel to zero
      because ``A`` is sparse there.  Those cancellation entries must be
      tiny (``max_cancellation``) rather than counted as structural errors.
    """
    rebuilt = reconstruct_factor(factor).tocoo()
    a_coo = matrix_csc.tocoo()
    a_fro = float(sparse.linalg.norm(matrix_csc, ord="fro"))

    # Structural check on the FACTOR pattern against the symbolic prediction.
    sym = factor.symbolic
    l_coo = factor.l_csc().tocoo()
    l_support = set(zip(l_coo.row.tolist(), l_coo.col.tolist()))
    predicted = set()
    for k in range(sym.n):
        for p in range(sym.col_ptr[k], sym.col_ptr[k + 1]):
            predicted.add((int(sym.row_idx[p]), k))
    factor_mismatches = len(l_support.symmetric_difference(predicted))

    # Structural support of A, used to separate cancellation at A's zeros
    # from genuine reconstruction error at A's stored entries.
    a_support = set(zip(a_coo.row.tolist(), a_coo.col.tolist()))

    diff = (rebuilt - matrix_csc).tocoo()
    d_fro = float(sparse.linalg.norm(diff, ord="fro"))

    cancellation = 0
    max_cancel = 0.0
    max_diff = 0.0
    for r, c, v in zip(rebuilt.row.tolist(), rebuilt.col.tolist(),
                       rebuilt.data.tolist()):
        if (r, c) not in a_support:
            cancellation += 1
            max_cancel = max(max_cancel, abs(float(v)))
    if diff.nnz:
        max_diff = float(np.max(np.abs(diff.data)))

    return ReconstructionEvidence(
        diff_norm_fro=d_fro,
        a_norm_fro=a_fro,
        relative_fro=d_fro / a_fro if a_fro > 0 else d_fro,
        max_abs_diff=max_diff,
        factor_pattern_mismatches=factor_mismatches,
        cancellation_positions=cancellation,
        max_cancellation=max_cancel,
    )
