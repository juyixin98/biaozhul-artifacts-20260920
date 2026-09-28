"""Pure mpmath matrix arithmetic and norms.

Everything here runs at the caller-selected mpmath precision. Keeping the
high-precision kernels in one place makes the precision contract explicit:
no NumPy float arrays ever flow into residual computation.
"""

from __future__ import annotations

from mpmath import matrix as mp_matrix
from mpmath import mpf


def make_matrix(rows: int, cols: int) -> mp_matrix:
    return mp_matrix(rows, cols)


def infinity_norm(mat: mp_matrix) -> mpf:
    """Maximum absolute row sum, exact at the active precision."""
    best = mpf(0)
    for i in range(mat.rows):
        total = mpf(0)
        for j in range(mat.cols):
            total += abs(mat[i, j])
        if total > best:
            best = total
    return best


def one_norm(mat: mp_matrix) -> mpf:
    """Maximum absolute column sum."""
    best = mpf(0)
    for j in range(mat.cols):
        total = mpf(0)
        for i in range(mat.rows):
            total += abs(mat[i, j])
        if total > best:
            best = total
    return best


def mat_mat(left: mp_matrix, right: mp_matrix) -> mp_matrix:
    if left.cols != right.rows:
        raise ValueError("mat_mat: shape mismatch")
    out = mp_matrix(left.rows, right.cols)
    for i in range(left.rows):
        for j in range(right.cols):
            acc = mpf(0)
            for k in range(left.cols):
                acc += left[i, k] * right[k, j]
            out[i, j] = acc
    return out


def residual_matrix(a: mp_matrix, x: mp_matrix, b: mp_matrix) -> mp_matrix:
    """r = b - A x evaluated with the *original* matrix A at high precision."""
    ax = mat_mat(a, x)
    out = mp_matrix(b.rows, b.cols)
    for i in range(b.rows):
        for j in range(b.cols):
            out[i, j] = b[i, j] - ax[i, j]
    return out
