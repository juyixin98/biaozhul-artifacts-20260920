"""Independent high-precision reference implemented with mpmath.

This module deliberately does NOT use the Krylov kernel under test. It
computes exp(tA)v by scaling-and-squaring with a Taylor series in
arbitrary-precision arithmetic, so it can serve as ground truth for the
kernel, segmentation, and service-level tests.
"""
from __future__ import annotations

import mpmath as mp
import numpy as np


def _max_abs_entry(matrix, n):
    return max(abs(matrix[i, j]) for i in range(n) for j in range(n))


def reference_expmv(A: np.ndarray, t: float, v: np.ndarray, dps: int = 60) -> np.ndarray:
    """Compute exp(tA)v in `dps`-digit arithmetic via scaling and squaring."""
    mp.mp.dps = dps
    n = A.shape[0]
    M = mp.matrix(A.tolist()) * mp.mpf(t)
    norm_inf = max(mp.fsum([abs(M[i, j]) for j in range(n)]) for i in range(n))
    threshold = mp.mpf("0.5")
    s = 0
    if norm_inf > threshold:
        s = int(mp.ceil(mp.log(norm_inf / threshold) / mp.log(2)))
    Ms = M / mp.mpf(2) ** s
    E = mp.eye(n)
    term = mp.eye(n)
    term_tol = mp.mpf(10) ** (-(dps - 10))
    k = 0
    while True:
        k += 1
        term = (term * Ms) / mp.mpf(k)
        E = E + term
        if _max_abs_entry(term, n) < term_tol:
            break
        if k > 10000:
            raise RuntimeError("reference Taylor series did not converge")
    for _ in range(s):
        E = E * E
    w = E * mp.matrix(v.tolist())
    return np.array([float(w[i]) for i in range(n)], dtype=np.float64)


def relative_error(computed: np.ndarray, reference: np.ndarray) -> float:
    denom = float(np.linalg.norm(reference))
    if denom == 0.0:
        return float(np.linalg.norm(computed))
    return float(np.linalg.norm(computed - reference) / denom)
