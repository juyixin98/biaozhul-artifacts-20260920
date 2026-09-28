"""Arnoldi iteration: builds an orthonormal Krylov basis without forming A.

Only a matrix-vector product is required, so A stays sparse (or implicit).
The projected problem lives entirely in the small Hessenberg matrix H.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class ArnoldiResult:
    """Result of one Arnoldi run.

    Attributes:
        basis: (n, k+1) orthonormal columns V.  Column k is the "next"
            vector; it is the zero vector after a happy breakdown.
        hessenberg: (k+1, k) upper Hessenberg matrix H.  Its square top
            ``H[:k, :k]`` is the projection of A onto the Krylov subspace;
            ``H[k, k-1]`` is the residual coupling h_{k+1,k} (0 on happy
            breakdown, meaning the subspace is A-invariant).
        beta: norm of the input vector; the first column is v / beta.
        happy_breakdown: True when the subspace became A-invariant, in
            which case the Krylov approximation is exact in exact arithmetic.
    """

    basis: np.ndarray
    hessenberg: np.ndarray
    beta: float
    happy_breakdown: bool

    @property
    def dim(self) -> int:
        return self.hessenberg.shape[1]


def arnoldi(matvec, v: np.ndarray, m_max: int, breakdown_tol: float = 1.0e-14) -> ArnoldiResult:
    """Run at most ``m_max`` Arnoldi steps starting from ``v``.

    ``matvec`` must compute A @ x.  Uses classical Gram-Schmidt with one
    reorthogonalisation pass for numerical robustness (fixed rule: exactly
    one pass, no iterative refinement).
    """
    n = v.shape[0]
    beta = float(np.linalg.norm(v))
    if beta == 0.0:
        raise ValueError("arnoldi requires a nonzero start vector")

    V = np.zeros((n, m_max + 1), dtype=np.float64)
    H = np.zeros((m_max + 1, m_max), dtype=np.float64)
    V[:, 0] = v / beta

    k = m_max
    happy = False
    for j in range(m_max):
        w = matvec(V[:, j])
        # classical Gram-Schmidt + one reorthogonalisation pass
        for _ in range(2):
            for i in range(j + 1):
                s = float(np.dot(V[:, i], w))
                H[i, j] += s
                w -= s * V[:, i]
        h_next = float(np.linalg.norm(w))
        H[j + 1, j] = h_next
        if h_next <= breakdown_tol * max(beta, 1.0):
            # happy breakdown: K_{j+1} is A-invariant
            H[j + 1, j] = 0.0
            k = j + 1
            happy = True
            break
        V[:, j + 1] = w / h_next

    return ArnoldiResult(
        basis=V[:, : k + 1],
        hessenberg=H[: k + 1, :k],
        beta=beta,
        happy_breakdown=happy,
    )
