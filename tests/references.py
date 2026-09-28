"""Independent reference implementations for tests.

Nothing in this module imports the service core: references are either
hand-derived analytic formulas or high-precision (mpmath, 50 decimal
digits) finite differences, so a bug in the core cannot silently
"agree with itself".
"""

from __future__ import annotations

import mpmath as mp
import numpy as np

# --- quadratic: f(x) = 0.5 x^T A x + b^T x ---------------------------------

QUAD_A = np.array(
    [
        [2.0, 0.5, -1.0],
        [0.5, 3.0, 0.25],
        [-1.0, 0.25, 1.5],
    ]
)
QUAD_B = np.array([0.3, -0.7, 1.2])


def quad_value(x: np.ndarray) -> float:
    return float(0.5 * x @ QUAD_A @ x + QUAD_B @ x)


def quad_grad(x: np.ndarray) -> np.ndarray:
    return QUAD_A @ x + QUAD_B


def quad_hvp(v: np.ndarray) -> np.ndarray:
    return QUAD_A @ v


# --- nonlinear: f(x) = sin(x0) * exp(x1) + x0^2 * x1 ------------------------


def nl_value(x: np.ndarray) -> float:
    return float(np.sin(x[0]) * np.exp(x[1]) + x[0] ** 2 * x[1])


def nl_grad(x: np.ndarray) -> np.ndarray:
    return np.array(
        [
            np.cos(x[0]) * np.exp(x[1]) + 2.0 * x[0] * x[1],
            np.sin(x[0]) * np.exp(x[1]) + x[0] ** 2,
        ]
    )


def nl_hessian(x: np.ndarray) -> np.ndarray:
    return np.array(
        [
            [-np.sin(x[0]) * np.exp(x[1]) + 2.0 * x[1], np.cos(x[0]) * np.exp(x[1]) + 2.0 * x[0]],
            [np.cos(x[0]) * np.exp(x[1]) + 2.0 * x[0], np.sin(x[0]) * np.exp(x[1])],
        ]
    )


# --- shared-subgraph function: z = x0*x1 + sin(x0); f = z^2 + z -------------
# Deliberately no hand-derived derivatives here: the reference is the
# mpmath high-precision Hessian/gradient below, so the test exercises the
# core's accumulation across shared nodes against an outside oracle.


def sharing_value_mp(x: list) -> mp.mpf:
    z = x[0] * x[1] + mp.sin(x[0])
    return z * z + z


def nl_value_mp(x: list) -> mp.mpf:
    return mp.sin(x[0]) * mp.exp(x[1]) + x[0] ** 2 * x[1]


# --- high-precision finite-difference oracles (mpmath) ----------------------

_DPS = 50


def mp_grad(f, x: np.ndarray) -> np.ndarray:
    """Central-difference gradient at 50-digit precision."""
    old = mp.mp.dps
    mp.mp.dps = _DPS
    try:
        xv = [mp.mpf(float(v)) for v in x]
        h = mp.mpf(10) ** (-15)
        n = len(xv)
        g = np.zeros(n)
        for i in range(n):
            xp = list(xv)
            xm = list(xv)
            xp[i] += h
            xm[i] -= h
            g[i] = float((f(xp) - f(xm)) / (2 * h))
        return g
    finally:
        mp.mp.dps = old


def mp_hessian(f, x: np.ndarray) -> np.ndarray:
    """Explicit full Hessian via central differences at 50-digit precision.

    h = 1e-12 gives truncation error O(h^2) ~ 1e-24 and roundoff
    O(eps_mp / h^2) ~ 1e-26, so the result is accurate to ~1e-20.
    """
    old = mp.mp.dps
    mp.mp.dps = _DPS
    try:
        xv = [mp.mpf(float(v)) for v in x]
        n = len(xv)
        h = mp.mpf(10) ** (-12)
        f0 = f(xv)
        H = np.zeros((n, n))
        for i in range(n):
            xp = list(xv)
            xm = list(xv)
            xp[i] += h
            xm[i] -= h
            H[i, i] = float((f(xp) - 2 * f0 + f(xm)) / (h * h))
            for j in range(i + 1, n):
                xpp = list(xv)
                xpm = list(xv)
                xmp = list(xv)
                xmm = list(xv)
                xpp[i] += h
                xpp[j] += h
                xpm[i] += h
                xpm[j] -= h
                xmp[i] -= h
                xmp[j] += h
                xmm[i] -= h
                xmm[j] -= h
                H[i, j] = H[j, i] = float(
                    (f(xpp) - f(xpm) - f(xmp) + f(xmm)) / (4 * h * h)
                )
        return H
    finally:
        mp.mp.dps = old
