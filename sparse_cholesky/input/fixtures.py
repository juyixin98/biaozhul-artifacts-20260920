"""Local synthetic sparse matrix fixtures.

All test/demo data is generated here -- no external accounts or real data are
involved.  Every generator returns lower-triangular COO triples plus metadata,
ready for :func:`sparse_cholesky.input.matrix.build_sparse_matrix`.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class COOTriples:
    n: int
    rows: np.ndarray
    cols: np.ndarray
    vals: np.ndarray
    name: str

    def keys(self) -> tuple[int, np.ndarray, np.ndarray, np.ndarray]:
        return self.n, self.rows, self.cols, self.vals


def _pack(n: int, pairs: list[tuple[int, int]], values: list[float],
          name: str) -> COOTriples:
    rows = np.array([i for i, _ in pairs], dtype=np.int64)
    cols = np.array([j for _, j in pairs], dtype=np.int64)
    vals = np.array(values, dtype=np.float64)
    return COOTriples(n=n, rows=rows, cols=cols, vals=vals, name=name)


def grid2d_laplacian(k: int, *, value: float = 4.0,
                     coupling: float = -1.0) -> COOTriples:
    """k x k five-point Laplacian on a regular grid (n = k*k).

    Diagonal 4 with four off-diagonal -1 couplings to grid neighbours.  With
    ``value=4`` and ``coupling=-1`` the smallest eigenvalue is strictly
    positive (the Neumann-ish interior matrix); it is SPD for k >= 2.
    """
    if k < 2:
        raise ValueError("grid side length must be >= 2")
    n = k * k
    pairs: list[tuple[int, int]] = []
    vals: list[float] = []
    for r in range(k):
        for c in range(k):
            node = r * k + c
            degree = 4
            if r == 0 or r == k - 1:
                degree -= 1
            if c == 0 or c == k - 1:
                degree -= 1
            pairs.append((node, node))
            # Interior value 4 keeps every diagonal equal to 4 regardless of
            # boundary degree -> strictly diagonally dominant, hence SPD.
            vals.append(value)
            if c > 0:
                pairs.append((node, node - 1))
                vals.append(coupling)
            if r > 0:
                pairs.append((node, node - k))
                vals.append(coupling)
    return _pack(n, pairs, vals, f"grid2d_laplacian_{k}x{k}")


def banded_spd(n: int, half_bandwidth: int, *, diag: float = 10.0,
               off: float = -1.0) -> COOTriples:
    """Symmetric banded SPD matrix with fixed half-bandwidth.

    Diagonal dominance (``diag`` large vs. two neighbours) guarantees SPD.
    """
    if half_bandwidth < 0 or half_bandwidth >= n:
        raise ValueError("half_bandwidth must be in [0, n)")
    pairs: list[tuple[int, int]] = []
    vals: list[float] = []
    for i in range(n):
        pairs.append((i, i))
        vals.append(diag)
        for bw in range(1, half_bandwidth + 1):
            j = i - bw
            if j >= 0:
                pairs.append((i, j))
                vals.append(off)
    return _pack(n, pairs, vals, f"banded_n{n}_bw{half_bandwidth}")


def tridiagonal_spd(n: int, *, diag: float = 2.0, off: float = -1.0) -> COOTriples:
    """Classic tridiagonal SPD matrix (2 on diagonal, -1 off)."""
    return banded_spd(n, 1, diag=diag, off=off)


def diagonal_spd(n: int, *, seed: int = 0) -> COOTriples:
    """Random positive diagonal matrix."""
    rng = np.random.default_rng(seed)
    vals = rng.uniform(0.5, 5.0, size=n)
    pairs = [(i, i) for i in range(n)]
    return _pack(n, pairs, list(vals), f"diagonal_n{n}")


def spd_with_dense_block(block: int, tail: int, *, seed: int = 1) -> COOTriples:
    """An SPD matrix containing a small fully dense block plus a diagonal tail.

    Used to verify fill-in is confined structurally: the dense block fills
    completely but nothing leaks into the tail.
    """
    rng = np.random.default_rng(seed)
    n = block + tail
    pairs: list[tuple[int, int]] = []
    vals: list[float] = []
    # Build an SPD dense block as B^T B + block*I, which is SPD.
    bmat = rng.standard_normal((block, block))
    dense = bmat @ bmat.T + block * np.eye(block)
    for i in range(n):
        for j in range(i + 1):
            if i < block:
                pairs.append((i, j))
                vals.append(float(dense[i, j]))
            elif j == i:
                pairs.append((i, j))
                vals.append(1.0)
    return _pack(n, pairs, vals, f"dense_block_b{block}_t{tail}")


def non_positive_definite_fixture(kind: str = "zero_pivot",
                                  n: int = 6) -> COOTriples:
    """Matrices that are symmetric but NOT positive definite.

    Kinds
    -----
    - ``zero_pivot``: first diagonal is zero (singular from the first step).
    - ``negative_diag``: a negative diagonal entry.
    - ``indefinite``: an interior negative pivot reached after real updates.
    """
    pairs = [(i, i) for i in range(n)]
    vals: list[float] = [2.0] * n

    if kind == "zero_pivot":
        vals[0] = 0.0
        for i in range(1, n):
            pairs.append((i, i - 1))
            vals.append(-1.0)
    elif kind == "negative_diag":
        vals[n // 2] = -3.0
        for i in range(1, n):
            pairs.append((i, i - 1))
            vals.append(-1.0)
    elif kind == "indefinite":
        # Diagonally dominant-looking but with one weak pivot: build a matrix
        # whose third Schur complement pivot is negative.
        pairs = []
        vals = []
        a = _indefinite_dense(n)
        for i in range(n):
            for j in range(i + 1):
                if a[i, j] != 0.0:
                    pairs.append((i, j))
                    vals.append(float(a[i, j]))
        return _pack(n, pairs, vals, f"indefinite_n{n}")
    else:
        raise ValueError(f"unknown non-PD fixture kind: {kind}")

    return _pack(n, pairs, vals, f"nonpd_{kind}_n{n}")


def _indefinite_dense(n: int) -> np.ndarray:
    """Construct a symmetric matrix with a guaranteed negative Schur pivot.

    Start from an SPD tridiagonal and subtract a rank-1 term so one
    eigenvalue becomes negative while sparsity widens slightly.
    """
    a = np.zeros((n, n))
    for i in range(n):
        a[i, i] = 2.0
        if i > 0:
            a[i, i - 1] = -1.0
            a[i - 1, i] = -1.0
    # Rank-1 modification v v^T with a large coefficient along v = ones makes
    # the matrix indefinite (eigenvalue along ones moves by n * scale).
    v = np.ones(n) / np.sqrt(n)
    scale = 6.0  # > largest eigenvalue (~4) -> one negative eigenvalue
    a = a - scale * np.outer(v, v)
    return a


def pattern_change_pair(*, seed: int = 7) -> tuple[COOTriples, COOTriples]:
    """Two matrices with identical values/n but different sparsity patterns.

    Used to prove cached symbolic structure is only reused on exact pattern
    match.
    """
    n = 8
    # Matrix A: tridiagonal.
    a = banded_spd(n, 1)
    # Matrix B: same diagonals but add one long-range edge (0, 7) lower (7,0).
    pairs_b = list(zip(a.rows.tolist(), a.cols.tolist()))
    vals_b = a.vals.tolist()
    pairs_b.append((n - 1, 0))
    vals_b.append(0.5)
    rows_b = np.array([p[0] for p in pairs_b], dtype=np.int64)
    cols_b = np.array([p[1] for p in pairs_b], dtype=np.int64)
    b = COOTriples(n=n, rows=rows_b,
                   cols=cols_b,
                   vals=np.array(vals_b, dtype=np.float64),
                   name="pattern_change_b")
    return a, b
