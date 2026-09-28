"""Low-precision factorizations and arbitrary-precision LU.

Stage design (each stage's precision is explicit and recorded):

* ``float32`` / ``float64``: SciPy LU with partial pivoting. The factors live
  only at that binary precision; corrections handed to the solver are cast
  down and solutions are cast back up, never mutating the caller's arrays.
  A binary factorization is declared unusable only on an *exact* zero pivot;
  tiny-but-nonzero pivots belong to merely ill-conditioned matrices that
  iterative refinement can still solve, and genuine singularity is already
  established by the high-precision rank probe.
* ``mp-<dps>``: Gaussian elimination with partial pivoting in pure mpmath at
  ``dps`` decimal digits, giving the ladder of arbitrary-precision fallbacks.
  Its singular test uses a pivot-ratio threshold expressed in that stage's own
  unit roundoff.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import scipy.linalg as sla
from mpmath import mpf

from . import mp_epsilon, workprec


@dataclass(frozen=True)
class BinaryLU:
    """Frozen SciPy LU factors at a fixed binary dtype."""

    kind: str  # "float32" | "float64"
    lu: np.ndarray
    piv: np.ndarray
    dtype: np.dtype
    singular: bool
    pivot_ratio: float
    n: int

    def solve_columns(self, columns: np.ndarray) -> np.ndarray:
        """Solve A X = columns. Inputs/outputs copied (never mutated)."""
        rhs = np.asarray(columns, dtype=self.dtype)
        if self.singular:
            raise np.linalg.LinAlgError(
                f"{self.kind} factorization is numerically singular"
            )
        return sla.lu_solve(
            (self.lu, self.piv), rhs, trans=0, overwrite_b=False, check_finite=False
        )


@dataclass(frozen=True)
class MPLU:
    """Partial-pivot LU at a fixed mpmath decimal precision."""

    kind: str  # "mp-30" etc.
    dps: int
    lu: list[list[mpf]]
    perm: list[int]
    singular: bool
    pivot_ratio: mpf
    n: int

    def solve_column(self, column: list[mpf]) -> list[mpf]:
        return _mp_solve(self, column, transpose=False)

    def solve_transpose_column(self, column: list[mpf]) -> list[mpf]:
        return _mp_solve(self, column, transpose=True)


def factor_binary(a_mp, dtype_name: str) -> BinaryLU:
    """Factor an mpmath matrix after rounding to float32/float64."""
    if dtype_name not in ("float32", "float64"):
        raise ValueError(f"unknown binary dtype {dtype_name}")
    dtype = np.dtype(dtype_name)
    n = a_mp.rows
    a_np = np.array(
        [[float(a_mp[i, j]) for j in range(n)] for i in range(n)], dtype=dtype
    )
    with np.errstate(divide="ignore", invalid="ignore"):
        lu, piv = sla.lu_factor(a_np, overwrite_a=False, check_finite=False)
    diag = np.abs(np.diag(lu))
    scale = float(np.max(np.abs(np.triu(lu)))) or 1.0
    pivot_ratio = float(diag.min() / scale) if diag.size else 0.0
    # At binary precision, "tiny but nonzero" pivots are the signature of a
    # merely very ill-conditioned matrix; iterative refinement handles those.
    # Only an exact zero pivot means the factorization cannot solve at all.
    # Genuine singularity is established beforehand by the high-precision probe.
    singular = bool(diag.size == 0 or diag.min() == 0.0)
    return BinaryLU(
        kind=dtype_name,
        lu=lu,
        piv=piv,
        dtype=dtype,
        singular=singular,
        pivot_ratio=pivot_ratio,
        n=n,
    )


def factor_mp(a_mp, dps: int, pivot_factor: mpf) -> MPLU:
    """Partial-pivot LU of an mpmath matrix at ``dps`` decimal digits."""
    n = a_mp.rows
    with workprec(dps):
        lu = [[a_mp[i, j] for j in range(n)] for i in range(n)]
        perm = list(range(n))
        max_u = mpf(0)
        for k in range(n):
            pivot_row = max(range(k, n), key=lambda i: abs(lu[i][k]))
            pivot = lu[pivot_row][k]
            if pivot_row != k:
                lu[k], lu[pivot_row] = lu[pivot_row], lu[k]
                perm[k], perm[pivot_row] = perm[pivot_row], perm[k]
                pivot = lu[k][k]
            if abs(pivot) > max_u:
                max_u = abs(pivot)
            if pivot != 0:
                for i in range(k + 1, n):
                    lu[i][k] = lu[i][k] / pivot
                    aik = lu[i][k]
                    for j in range(k + 1, n):
                        lu[i][j] = lu[i][j] - aik * lu[k][j]
        scale = max_u or mpf(1)
        diag_abs = [abs(lu[k][k]) for k in range(n)]
        pivot_ratio = min(diag_abs, default=mpf(0)) / scale
        threshold = mp_epsilon(dps) * pivot_factor
        singular = any(v == 0 for v in diag_abs) or pivot_ratio < threshold
        return MPLU(
            kind=f"mp-{dps}",
            dps=dps,
            lu=lu,
            perm=perm,
            singular=singular,
            pivot_ratio=pivot_ratio,
            n=n,
        )


def _mp_solve(handle: MPLU, column: list[mpf], transpose: bool) -> list[mpf]:
    """Forward/back substitution on the stored LU (optionally with A^T)."""
    n = handle.n
    with workprec(handle.dps):
        rhs = [mpf(v) for v in column]
        if transpose:
            # A = P^T L U  =>  A^T = U^T L^T P.  Solve A^T y = b:
            #   U^T v = b  (back-sub branch), then  L^T w = v, then y = P^T w.
            v = _back_sub(handle, rhs, transpose_u=True)
            w = _forward_unit(handle, v, transpose_l=True)
            out = [mpf(0)] * n
            for k, row in enumerate(handle.perm):
                out[row] = w[k]
            return out
        # P A = L U: apply permutation, forward L, back U.
        pb = [rhs[row] for row in handle.perm]
        y = _forward_unit(handle, pb, transpose_l=False)
        return _back_sub(handle, y, transpose_u=False)


def _forward_unit(handle: MPLU, rhs: list[mpf], transpose_l: bool) -> list[mpf]:
    n = handle.n
    lu = handle.lu
    y = [mpf(0)] * n
    if not transpose_l:
        for i in range(n):
            acc = rhs[i]
            for j in range(i):
                acc -= lu[i][j] * y[j]
            y[i] = acc  # L has unit diagonal
    else:  # solve L^T y = rhs; L^T is upper triangular with unit diagonal
        for i in range(n - 1, -1, -1):
            acc = rhs[i]
            for j in range(i + 1, n):
                acc -= lu[j][i] * y[j]
            y[i] = acc
    return y


def _back_sub(handle: MPLU, rhs: list[mpf], transpose_u: bool) -> list[mpf]:
    n = handle.n
    lu = handle.lu
    x = [mpf(0)] * n
    if not transpose_u:
        for i in range(n - 1, -1, -1):
            acc = rhs[i]
            for j in range(i + 1, n):
                acc -= lu[i][j] * x[j]
            x[i] = acc / lu[i][i]
    else:  # solve U^T x = rhs; U^T is lower triangular
        for i in range(n):
            acc = rhs[i]
            for j in range(i):
                acc -= lu[j][i] * x[j]
            x[i] = acc / lu[i][i]
    return x


def lu_solve_column(handle: MPLU, column: list[mpf]) -> list[mpf]:
    return handle.solve_column(column)


def lu_solve_transpose_column(handle: MPLU, column: list[mpf]) -> list[mpf]:
    return handle.solve_transpose_column(column)
