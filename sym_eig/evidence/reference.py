"""Independent reference oracles.

Reference answers are never produced by the kernel under test:

* :func:`mpmath_reference` runs mpmath's *own* arbitrary-precision symmetric
  eigensolver (a different implementation at configurable precision), and
* :func:`scipy_reference` calls LAPACK (``dsyevd``) through SciPy, the
  standard mature-library answer.

Both are external to the Householder/QR kernel in
``sym_eig.numerical.kernel`` and are therefore legitimate independent
oracles.
"""

from dataclasses import dataclass

import mpmath as mp
import numpy as np
import scipy.linalg


@dataclass(frozen=True)
class ReferenceResult:
    eigenvalues: np.ndarray
    eigenvectors: np.ndarray
    source: str
    precision_dps: int | None


def mpmath_reference(
    matrix: np.ndarray, dps: int = 50
) -> ReferenceResult:
    """High-precision eigendecomposition via mpmath's built-in solver."""
    n = matrix.shape[0]
    with mp.workdps(dps):
        high = mp.matrix(n, n)
        for i in range(n):
            for j in range(n):
                high[i, j] = mp.mpf(float(matrix[i, j]))
        w_mp, q_mp = mp.eigh(high)  # ascending eigenvalues, Q columns
        w = np.array([float(w_mp[i, 0]) for i in range(n)], dtype=np.float64)
        q = np.array(
            [[float(q_mp[i, j]) for j in range(n)] for i in range(n)],
            dtype=np.float64,
        )
    return ReferenceResult(w, q, source="mpmath.eigh", precision_dps=dps)


def scipy_reference(matrix: np.ndarray) -> ReferenceResult:
    """Mature-library eigendecomposition via LAPACK dsyevd."""
    w, q = scipy.linalg.eigh(
        matrix, overwrite_a=False, check_finite=True,
        driver="evd",
    )
    return ReferenceResult(
        np.asarray(w, dtype=np.float64),
        np.asarray(q, dtype=np.float64),
        source="scipy.linalg.eigh(dsyevd)",
        precision_dps=None,
    )
