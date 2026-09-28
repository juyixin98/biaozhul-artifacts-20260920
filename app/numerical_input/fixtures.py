"""Synthetic local fixtures.

No external data or accounts: every fixture is generated deterministically
here. These cover the four required families:

* grid sparse  - 2-D five-point Laplacian (plus SPD shift),
* banded       - 1-D tridiagonal Laplacian,
* pattern swap - same dimension/nnz, *different* sparsity pattern,
* non-SPD      - negative diagonal, indefinite and singular matrices.

Each fixture returns raw COO arrays plus a right-hand side, so callers
(including the tests) drive the full validation pipeline themselves.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class CooFixture:
    name: str
    n: int
    rows: np.ndarray
    cols: np.ndarray
    vals: np.ndarray
    rhs: np.ndarray
    # For failure fixtures: expected elimination index (original ordering)
    expected_bad_pivot: int | None = None
    spd: bool = True
    description: str = ""


def _insert(rows, cols, vals, i, j, v):
    rows.append(i)
    cols.append(j)
    vals.append(float(v))


def grid_laplacian(nx: int = 7, ny: int = 6, *, shift: float = 1.0,
                   name: str = "grid_5pt") -> CooFixture:
    """Dirichlet 2-D 5-point Laplacian + shift*I.

    L has +4 on the diagonal and -1 to grid neighbours; ``shift`` makes it
    strictly positive definite (the pure interior graph Laplacian is
    singular). RHS is a smooth analytic field so the dense reference is
    meaningful.
    """
    n = nx * ny
    rows: list[int] = []
    cols: list[int] = []
    vals: list[float] = []

    def idx(x: int, y: int) -> int:
        return y * nx + x

    for y in range(ny):
        for x in range(nx):
            k = idx(x, y)
            _insert(rows, cols, vals, k, k, 4.0 + shift)
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                xx, yy = x + dx, y + dy
                if 0 <= xx < nx and 0 <= yy < ny:
                    _insert(rows, cols, vals, k, idx(xx, yy), -1.0)
    # RHS: sampled smooth function.
    gx = np.arange(nx) + 1
    gy = np.arange(ny) + 1
    X, Y = np.meshgrid(gx, gy)
    rhs = (np.sin(0.5 * X) * np.cos(0.3 * Y)).reshape(-1)
    return CooFixture(name, n,
                      np.asarray(rows, dtype=np.int64),
                      np.asarray(cols, dtype=np.int64),
                      np.asarray(vals, dtype=np.float64),
                      rhs,
                      description=f"{nx}x{ny} 5-point Laplacian + {shift}I")


def banded(n: int = 50, half_band: int = 1, *,
           name: str = "banded_tri") -> CooFixture:
    """Symmetric positive definite band matrix.

    Diagonal 4, off-diagonals -1 within ``half_band``. The tridiagonal
    case (half_band=1) is the 1-D Dirichlet Laplacian, whose smallest
    eigenvalue is strictly positive.
    """
    rows: list[int] = []
    cols: list[int] = []
    vals: list[float] = []
    for i in range(n):
        _insert(rows, cols, vals, i, i, 4.0)
        for k in range(1, half_band + 1):
            if i + k < n:
                _insert(rows, cols, vals, i, i + k, -1.0)
                _insert(rows, cols, vals, i + k, i, -1.0)
    rhs = np.linspace(1.0, 2.0, n) * ((np.arange(n) % 3) - 1)
    return CooFixture(name, n,
                      np.asarray(rows, dtype=np.int64),
                      np.asarray(cols, dtype=np.int64),
                      np.asarray(vals, dtype=np.float64),
                      rhs,
                      description=f"n={n} half-band {half_band} SPD band")


def arrowhead(n: int = 20, *, name: str = "arrowhead") -> CooFixture:
    """Arrowhead pattern: dense first row/column plus a diagonal."""
    rows: list[int] = []
    cols: list[int] = []
    vals: list[float] = []
    _insert(rows, cols, vals, 0, 0, float(n) + 1.0)
    for i in range(1, n):
        _insert(rows, cols, vals, 0, i, 0.5)
        _insert(rows, cols, vals, i, 0, 0.5)
        _insert(rows, cols, vals, i, i, 4.0)
    rhs = np.ones(n)
    rhs[::2] = -1.0
    return CooFixture(name, n,
                      np.asarray(rows, dtype=np.int64),
                      np.asarray(cols, dtype=np.int64),
                      np.asarray(vals, dtype=np.float64),
                      rhs,
                      description=f"n={n} SPD arrowhead")


def pattern_swap_pair(n: int = 12):
    """Two SPD matrices with identical n and nnz but different patterns.

    Used to prove symbol reuse requires *complete* pattern equality.
    Returns (fixture_a, fixture_b).
    """
    # A: tridiagonal
    a = banded(n, 1, name="swap_tridiag")
    # B: same nnz budget moved into an arrowhead + tail pairs.
    rows: list[int] = []
    cols: list[int] = []
    vals: list[float] = []
    _insert(rows, cols, vals, 0, 0, 8.0)
    # consume the same number of off-diagonal symmetric pairs as A (n-1)
    pairs = n - 1
    placed = 0
    i = 1
    # first connect 0 to as many nodes as needed pattern-wise,
    # then pair remaining nodes arbitrarily with long edges
    while placed < pairs:
        if placed < n - 2 and i < n:
            _insert(rows, cols, vals, 0, i, 0.25)
            _insert(rows, cols, vals, i, 0, 0.25)
            i += 1
            placed += 1
        else:
            # last pair: long edge (1, n-1)
            _insert(rows, cols, vals, 1, n - 1, -0.25)
            _insert(rows, cols, vals, n - 1, 1, -0.25)
            placed += 1
    for k in range(1, n):
        _insert(rows, cols, vals, k, k, 6.0)
    rhs = np.cos(np.arange(n) * 0.4)
    b = CooFixture("swap_arrow", n,
                   np.asarray(rows, dtype=np.int64),
                   np.asarray(cols, dtype=np.int64),
                   np.asarray(vals, dtype=np.float64),
                   rhs,
                   description="same n/nnz as tridiag, different pattern")
    return a, b


def negative_diagonal(n: int = 8, bad: int = 3, *,
                      name: str = "nonspd_negative_diag") -> CooFixture:
    """SPD-looking tridiagonal with one negative diagonal entry."""
    rows: list[int] = []
    cols: list[int] = []
    vals: list[float] = []
    for i in range(n):
        _insert(rows, cols, vals, i, i, -1.0 if i == bad else 4.0)
        if i + 1 < n:
            _insert(rows, cols, vals, i, i + 1, -1.0)
            _insert(rows, cols, vals, i + 1, i, -1.0)
    return CooFixture(name, n,
                      np.asarray(rows, dtype=np.int64),
                      np.asarray(cols, dtype=np.int64),
                      np.asarray(vals, dtype=np.float64),
                      np.ones(n),
                      expected_bad_pivot=bad, spd=False,
                      description=f"negative diagonal at index {bad}")


def indefinite_3x3(*, name: str="nonspd_indefinite") -> CooFixture:
    """Classic indefinite matrix: first pivot fine, second one negative.

    A = [[1, 2, 0], [2, 1, 0], [0, 0, 1]] gives d_2 = 1 - 4 = -3.
    Expected failing pivot (0-based, original ordering): index 1.
    """
    rows: list[int] = []
    cols: list[int] = []
    vals: list[float] = []
    for i, j, v in [(0, 0, 1), (0, 1, 2), (1, 0, 2), (1, 1, 1),
                    (2, 2, 1)]:
        rows.append(i)
        cols.append(j)
        vals.append(float(v))
    return CooFixture(name, 3,
                      np.asarray(rows, dtype=np.int64),
                      np.asarray(cols, dtype=np.int64),
                      np.asarray(vals, dtype=np.float64),
                      np.ones(3),
                      expected_bad_pivot=1, spd=False,
                      description="indefinite; pivot index 1 is -3")


def singular_matrix(n: int = 6, zero_at: int = 4, *,
                    name: str = "singular_zero_pivot") -> CooFixture:
    """Symmetric matrix that develops a zero pivot after elimination."""
    rows: list[int] = []
    cols: list[int] = []
    vals: list[float] = []
    # Rank-deficient construction: two identical rows (0 and zero_at share
    # the same diagonal block pattern) forces a zero Schur-complement pivot.
    for i in range(n):
        _insert(rows, cols, vals, i, i, 2.0)
    _insert(rows, cols, vals, 0, zero_at, 2.0)
    _insert(rows, cols, vals, zero_at, 0, 2.0)
    # diag at zero_at is 2 with offdiag 2 -> after eliminating 0,
    # Schur complement 2 - 4/2 = 0.
    return CooFixture(name, n,
                      np.asarray(rows, dtype=np.int64),
                      np.asarray(cols, dtype=np.int64),
                      np.asarray(vals, dtype=np.float64),
                      np.ones(n),
                      expected_bad_pivot=zero_at, spd=False,
                      description=f"zero Schur pivot at index {zero_at}")


ALL_BUILDERS = [
    lambda: grid_laplacian(),
    lambda: banded(),
    lambda: arrowhead(),
    lambda: negative_diagonal(),
    lambda: indefinite_3x3(),
    lambda: singular_matrix(),
]
