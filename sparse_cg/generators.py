"""Generators for sparse SPD systems whose exact solution is known.

Given a synthetic matrix A and a chosen exact solution ``x_true`` we set
``b = A x_true``, so a solver's accuracy can be judged by *comparing
residuals* (and the error against x_true) rather than just iteration counts.

All matrices are symmetric positive definite by construction.
"""

from __future__ import annotations

import numpy as np

from .csr import CSRMatrix


def laplacian_1d(n: int, *, h: float = 1.0) -> tuple[CSRMatrix, np.ndarray]:
    """Tridiagonal SPD matrix of the 1-D Dirichlet Laplacian.

    A = (1/h^2) * tridiag(-1, 2, -1), size n.  Eigenvalues
    lambda_k = (4/h^2) sin^2(k pi / (2(n+1))), k = 1..n, so condition number
    kappa ~ (2/pi^2)(n+1)^2 -- mildly ill-conditioned at n=10^4 (~2e6).
    """
    if n < 1:
        raise ValueError("n must be >= 1")
    factor = 1.0 / (h * h)
    rows: list[int] = []
    cols: list[int] = []
    vals: list[float] = []
    for i in range(n):
        rows.append(i)
        cols.append(i)
        vals.append(2.0 * factor)
        if i > 0:
            rows.extend((i, i - 1))
            cols.extend((i - 1, i))
            vals.extend((-factor, -factor))
    A = CSRMatrix.from_triplets(n, rows, cols, vals)
    k = np.arange(1, n + 1, dtype=np.float64)
    eig = 4.0 * factor * np.sin(k * np.pi / (2.0 * (n + 1))) ** 2
    return A, eig


def spd_from_mass_spring(n: int, *, stiffness: float = 1.0,
                         mass: float = 0.1,
                         seed: int = 0) -> tuple[CSRMatrix, np.ndarray]:
    """SPD matrix of a 1-D mass-spring chain: A = stiffness*L + mass*I.

    Strictly diagonally dominant; always easy for CG.  Returns (A, diag_of_A).
    """
    rng = np.random.default_rng(seed)
    rows: list[int] = []
    cols: list[int] = []
    vals: list[float] = []
    diag = np.empty(n, dtype=np.float64)
    for i in range(n):
        d = 2.0 * stiffness + mass
        diag[i] = d
        rows.append(i)
        cols.append(i)
        vals.append(d)
        if i > 0:
            # small random but *symmetric* coupling
            c = -stiffness * (0.5 + 0.5 * float(rng.random()))
            rows.extend((i, i - 1))
            cols.extend((i - 1, i))
            vals.extend((c, c))
    A = CSRMatrix.from_triplets(n, rows, cols, vals)
    return A, diag


def diagonal_scaled_laplacian(n: int, kappa: float
                              ) -> tuple[CSRMatrix, np.ndarray]:
    """Laplacian + epsilon*I whose condition number is *exactly* controllable.

    For the 1-D Dirichlet Laplacian L (without the 1/h^2 factor) the
    eigenvalues are::

        lambda_k(L) = 4 sin^2(k pi / (2(n+1))), k = 1..n.

    Adding ``epsilon * I`` shifts the spectrum to
    [epsilon + lambda_min, epsilon + lambda_max].  Choosing
    epsilon = (lambda_max - kappa*lambda_min) / (kappa - 1) makes
    kappa(A) = kappa exactly (for kappa > kappa(L)).

    Returns (A, eigenvalues) with the exact eigenvalue list.
    """
    lam = np.array(
        [4.0 * np.sin(k * np.pi / (2.0 * (n + 1))) ** 2
         for k in range(1, n + 1)],
        dtype=np.float64,
    )
    lam_min, lam_max = float(lam.min()), float(lam.max())
    kappa_l = lam_max / lam_min
    if kappa <= kappa_l:
        epsilon = 0.0
    else:
        epsilon = (lam_max - kappa * lam_min) / (kappa - 1.0)

    rows: list[int] = []
    cols: list[int] = []
    vals: list[float] = []
    for i in range(n):
        rows.append(i)
        cols.append(i)
        vals.append(2.0 + epsilon)
        if i > 0:
            rows.extend((i, i - 1))
            cols.extend((i - 1, i))
            vals.extend((-1.0, -1.0))
    A = CSRMatrix.from_triplets(n, rows, cols, vals)
    return A, lam + epsilon


def make_rhs_from_exact_solution(A: CSRMatrix, x_true: np.ndarray
                                 ) -> tuple[np.ndarray, np.ndarray]:
    """Return (b, x_true) with b = A x_true."""
    return A.matvec(x_true), np.asarray(x_true, dtype=np.float64)


def smooth_x(n: int) -> np.ndarray:
    """Smooth exact solution: x_i = sin(pi i / (n+1))."""
    i = np.arange(1, n + 1, dtype=np.float64)
    return np.sin(np.pi * i / (n + 1))


def random_x(n: int, *, seed: int = 2, scale: float = 1.0) -> np.ndarray:
    """Random exact solution from a fixed seed (reproducible)."""
    rng = np.random.default_rng(seed)
    return scale * rng.standard_normal(n)
