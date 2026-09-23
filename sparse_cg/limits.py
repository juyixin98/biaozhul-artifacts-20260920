"""Hard input-range limits shared by the library and the JSON API.

These bounds deliberately target *small-to-medium* problems.  Everything is
pure Python/NumPy; going past these limits risks impractically slow solves,
so requests beyond them are rejected with a precise error instead of being
accepted silently.
"""

from __future__ import annotations

#: Maximum matrix dimension (matrix must be square).
MAX_N: int = 10_000

#: Maximum number of stored entries (nnz), including the diagonal.
MAX_NNZ: int = 500_000

#: Every matrix / vector component must satisfy |value| <= MAX_ABS_VALUE.
#: Chosen well below the overflow threshold (~1.8e308) so that products and
#: dot products in the solver stay finite even after squaring.
MAX_ABS_VALUE: float = 1.0e100

#: Smallest accepted convergence / symmetry tolerance.  Anything tighter is
#: unreachable in double precision for nontrivial systems.
MIN_TOL: float = 1.0e-14

#: Largest accepted tolerance (a tolerance near 1 makes no sense for CG).
MAX_TOL: float = 1.0e-2

#: Upper bound the API will ever accept for max_iter.
MAX_ITER_CAP: int = 100_000

#: Default multiplier applied to n when the caller omits max_iter, capped by
#: MAX_ITER_CAP (CG on an n x n SPD system needs at most n exact iterations).
DEFAULT_MAX_ITER_FACTOR: int = 10
