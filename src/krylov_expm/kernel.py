"""Arnoldi Krylov kernel for a single exp(tA)v step (no segmentation here)."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import scipy.linalg

from .config import SolverConfig
from .error_estimate import endpoint_residual_norm, integrated_error_estimate


@dataclass
class ArnoldiResult:
    basis: np.ndarray  # (n, m_max+1); first `dim` (+1 if no breakdown) columns valid
    hessenberg: np.ndarray  # (m_max+1, m_max) upper Hessenberg
    beta: float
    dim: int
    happy_breakdown: bool
    matvec_count: int


@dataclass
class KrylovStepResult:
    vector: np.ndarray
    krylov_dim: int
    happy_breakdown: bool
    subspace_residual_norm: float
    error_estimate: float
    matvec_count: int


def arnoldi(matvec, v: np.ndarray, m_max: int, breakdown_tol: float) -> ArnoldiResult:
    """Classical Arnoldi with one full reorthogonalization pass."""
    n = v.size
    basis = np.zeros((n, m_max + 1))
    h = np.zeros((m_max + 1, m_max))
    beta = float(np.linalg.norm(v))
    basis[:, 0] = v / beta
    matvec_count = 0
    happy = False
    dim = m_max
    for j in range(m_max):
        w = matvec(basis[:, j])
        matvec_count += 1
        for i in range(j + 1):
            hij = float(np.dot(basis[:, i], w))
            h[i, j] = hij
            w = w - hij * basis[:, i]
        for i in range(j + 1):  # reorthogonalization pass
            corr = float(np.dot(basis[:, i], w))
            h[i, j] += corr
            w = w - corr * basis[:, i]
        h_next = float(np.linalg.norm(w))
        h[j + 1, j] = h_next
        if h_next <= breakdown_tol:
            happy = True
            dim = j + 1
            break
        basis[:, j + 1] = w / h_next
    return ArnoldiResult(
        basis=basis,
        hessenberg=h,
        beta=beta,
        dim=dim,
        happy_breakdown=happy,
        matvec_count=matvec_count,
    )


def krylov_step(matrix, v: np.ndarray, t: float, config: SolverConfig) -> KrylovStepResult:
    """One Krylov step: w ~= exp(tA)v via exp of the small dense Hessenberg."""
    ar = arnoldi(lambda x: matrix @ x, v, config.max_krylov_dim, config.breakdown_tol)
    k = ar.dim
    hk = ar.hessenberg[:k, :k]
    exp_th = scipy.linalg.expm(t * hk)
    w = ar.beta * (ar.basis[:, :k] @ exp_th[:, 0])
    if ar.happy_breakdown:
        # Krylov subspace is A-invariant: the approximation is exact.
        residual = 0.0
        estimate = 0.0
    else:
        h_next = ar.hessenberg[k, k - 1]
        residual = endpoint_residual_norm(ar.beta, h_next, exp_th[k - 1, 0])
        estimate = integrated_error_estimate(
            ar.beta, h_next, hk, t, config.quadrature_points
        )
    return KrylovStepResult(
        vector=w,
        krylov_dim=k,
        happy_breakdown=ar.happy_breakdown,
        subspace_residual_norm=residual,
        error_estimate=estimate,
        matvec_count=ar.matvec_count,
    )
