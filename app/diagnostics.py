"""NumPy-based high-precision diagnostics.

The solving path (app.pool) is exact integer arithmetic. This module is a
*diagnostic only*: it re-evaluates the invariant residual with NumPy
extended-precision floats as an independent cross-check. Its output is
reported to callers but never influences the quoted integers.
"""

from __future__ import annotations

import numpy as np

from .pool import N_COINS


def invariant_residual_float(x: int, y: int, d: int, amplification: int) -> float:
    """|Ann*(x+y) + D - Ann*D - D**3/(4xy)| evaluated in np.longdouble.

    np.longdouble is 80-bit extended precision on x86-64 Linux (platform
    dependent; documented as a diagnostic, not a guarantee).
    """
    ann = np.longdouble(amplification * N_COINS**N_COINS)
    xl, yl, dl = np.longdouble(x), np.longdouble(y), np.longdouble(d)
    lhs = ann * (xl + yl) + dl
    rhs = ann * dl + dl**3 / (np.longdouble(4) * xl * yl)
    return float(abs(lhs - rhs))
