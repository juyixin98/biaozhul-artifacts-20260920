"""Local synthetic test fixtures.

All matrices are generated deterministically from fixed seeds - no external
services or real business data. Expected answers are produced by mature
libraries independent of the kernel under test:

* SciPy/LAPACK (``dsyevd``) for general reference answers, and
* mpmath arbitrary precision for small, high-confidence answers.

The kernel in ``sym_eig.numerical`` is never used to generate its own
expected outputs.
"""

from dataclasses import dataclass

import mpmath as mp
import numpy as np
import scipy.linalg


@dataclass(frozen=True)
class SymmetricCase:
    name: str
    matrix: np.ndarray
    expected_eigenvalues: np.ndarray
    expected_vectors: np.ndarray
    expected_clusters: tuple[tuple[int, float], ...]
    description: str


def _orthogonal(rng: np.random.Generator, n: int) -> np.ndarray:
    return np.linalg.qr(rng.standard_normal((n, n)))[0]


def _with_spectrum(
    name: str, eigenvalues: np.ndarray, seed: int, description: str,
    cluster_rtol: float = 1e-8,
) -> SymmetricCase:
    rng = np.random.default_rng(seed)
    q = _orthogonal(rng, len(eigenvalues))
    matrix = (q * eigenvalues) @ q.T
    w, v = scipy.linalg.eigh(matrix)
    clusters = _expected_clusters(w, cluster_rtol)
    return SymmetricCase(name, matrix, w, v, clusters, description)


def _expected_clusters(
    w: np.ndarray, cluster_rtol: float
) -> tuple[tuple[int, float], ...]:
    groups: list[int] = []
    out: list[tuple[int, float]] = []
    start = 0
    for i in range(1, len(w) + 1):
        if i == len(w):
            out.append((i - start, float(np.mean(w[start:i]))))
            break
        local_scale = max(1.0, float(abs(w[i - 1])), float(abs(w[i])))
        if w[i] - w[i - 1] > cluster_rtol * local_scale:
            out.append((i - start, float(np.mean(w[start:i]))))
            start = i
    return tuple(out)


def case_diagonal() -> SymmetricCase:
    eigenvalues = np.array([-3.0, -1.0, 0.5, 2.0, 7.0])
    matrix = np.diag(eigenvalues)
    return SymmetricCase(
        name="diagonal",
        matrix=matrix,
        expected_eigenvalues=np.sort(eigenvalues),
        expected_vectors=np.eye(5)[:, np.argsort(eigenvalues)],
        expected_clusters=((1, -3.0), (1, -1.0), (1, 0.5), (1, 2.0), (1, 7.0)),
        description="Pure diagonal: eigenvalues are the entries, vectors "
                    "standard basis.",
    )


def case_repeated_spectrum() -> SymmetricCase:
    return _with_spectrum(
        "repeated_spectrum",
        np.array([1.0, 1.0, 1.0, 2.0, 3.5, 3.5, 5.0]),
        seed=101,
        description="Spectrum (1,1,1,2,3.5,3.5,5): triple and double "
                    "eigenvalues; vectors must be compared as subspaces.",
    )


def case_near_degenerate() -> SymmetricCase:
    return _with_spectrum(
        "near_degenerate",
        np.array([1.0, 1.0 + 1.0e-12, 4.0, 4.0 + 2.0e-13, 9.0]),
        seed=202,
        description="Gaps of 1e-12..2e-13: indistinguishable vectors at "
                    "float64 precision; must cluster into subspaces.",
    )


def case_widely_separated_scales() -> SymmetricCase:
    # Dense (orthogonally rotated), so the tiny eigenvalues are embedded in
    # 1e8-scale off-diagonal entries. This is the genuinely ill-conditioned
    # situation: the small eigenvalues/eigenvectors are limited by the
    # float64 resolution of the large entries, not by the solver.
    return _with_spectrum(
        "widely_separated_scales",
        np.array([-1.0e8, 1.0, 1.0e-6]),
        seed=303,
        description="Dense spectrum spanning 14 orders of magnitude; checks "
                    "relative/scale-aware gates and the conditioning bound.",
    )


def case_tridiagonal_laplacian(n: int = 12) -> SymmetricCase:
    off = np.ones(n - 1)
    matrix = 2.0 * np.eye(n) - np.diag(off, 1) - np.diag(off, -1)
    w, v = scipy.linalg.eigh(matrix)
    return SymmetricCase(
        name="tridiagonal_laplacian",
        matrix=matrix,
        expected_eigenvalues=w,
        expected_vectors=v,
        expected_clusters=tuple((1, float(x)) for x in w),
        description="Path-graph Laplacian; unreduced tridiagonal that needs "
                    "several QR sweeps (used for the budget test).",
    )


def case_random_symmetric(n: int, seed: int) -> SymmetricCase:
    rng = np.random.default_rng(seed)
    m = rng.standard_normal((n, n))
    matrix = m + m.T
    w, v = scipy.linalg.eigh(matrix)
    return SymmetricCase(
        name=f"random_symmetric_n{n}_seed{seed}",
        matrix=matrix,
        expected_eigenvalues=w,
        expected_vectors=v,
        expected_clusters=tuple((1, float(x)) for x in w),
        description="Generic dense random symmetric matrix.",
    )


def mpmath_expected(matrix: np.ndarray, dps: int = 50) -> tuple[np.ndarray, np.ndarray]:
    """Independent high-precision expected answer via mpmath (not the kernel)."""
    n = matrix.shape[0]
    with mp.workdps(dps):
        high = mp.matrix(n, n)
        for i in range(n):
            for j in range(n):
                high[i, j] = mp.mpf(float(matrix[i, j]))
        w_mp, q_mp = mp.eigh(high)
        w = np.array([float(w_mp[i, 0]) for i in range(n)])
        q = np.array(
            [[float(q_mp[i, j]) for j in range(n)] for i in range(n)]
        )
    return w, q


ALL_CASES = [
    case_diagonal(),
    case_repeated_spectrum(),
    case_near_degenerate(),
    case_widely_separated_scales(),
    case_tridiagonal_laplacian(),
    case_random_symmetric(6, 404),
    case_random_symmetric(13, 505),
]
