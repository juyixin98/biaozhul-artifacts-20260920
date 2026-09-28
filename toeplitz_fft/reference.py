"""Independent reference implementations used to judge the FFT kernel.

These live outside the kernel on purpose: the dense path builds the full
Toeplitz matrix by direct index arithmetic and multiplies with BLAS, and the
mpmath path recomputes the product in 50-digit arbitrary precision. Neither
shares code with the FFT kernel, so agreement is real evidence.
"""

from __future__ import annotations

import numpy as np
from mpmath import mp, mpf, mpc


def dense_toeplitz(c: np.ndarray, r: np.ndarray) -> np.ndarray:
    """Materialize the full m x n Toeplitz matrix from first column and row.

    T[i, j] = c[i - j] if i >= j else r[j - i]. Built by pure index
    arithmetic; does not use the FFT kernel or scipy.linalg.toeplitz.
    """
    c = np.asarray(c)
    r = np.asarray(r)
    m, n = c.shape[0], r.shape[0]
    i, j = np.indices((m, n))
    d = i - j
    return np.where(d >= 0, c[np.clip(d, 0, None)], r[np.clip(-d, 0, None)])


def dense_matvec(c: np.ndarray, r: np.ndarray, x: np.ndarray) -> np.ndarray:
    """Direct O(m*n) reference: build T explicitly, then T @ x."""
    return dense_toeplitz(c, r) @ np.asarray(x)


def dense_matmat(c: np.ndarray, r: np.ndarray, X: np.ndarray) -> np.ndarray:
    """Direct O(m*n*k) reference for a batch of column vectors."""
    return dense_toeplitz(c, r) @ np.asarray(X)


def mpmath_matvec(c, r, x, dps: int = 50) -> list:
    """Arbitrary-precision reference for a single vector.

    Returns a list of mpf/mpc values. Intended for small cross-check cases;
    complexity is O(m*n) big-number multiplications.
    """
    old_dps = mp.dps
    mp.dps = dps
    try:
        def conv(v):
            z = complex(v)
            return mpc(z.real, z.imag) if z.imag != 0 else mpf(z.real)

        cc = [conv(v) for v in np.asarray(c).ravel()]
        rr = [conv(v) for v in np.asarray(r).ravel()]
        xx = [conv(v) for v in np.asarray(x).ravel()]
        m, n = len(cc), len(rr)
        out = []
        for i in range(m):
            acc = mpf(0)
            for j in range(n):
                t_ij = cc[i - j] if i >= j else rr[j - i]
                acc += t_ij * xx[j]
            out.append(acc)
        return out
    finally:
        mp.dps = old_dps
