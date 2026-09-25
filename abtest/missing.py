"""Missing-value strategies applied per group before analysis.

Strategies:
- ``DROP``: complete-case analysis; NaN observations are removed per group.
- ``IMPUTE_MEAN``: NaN observations are replaced by the observed (non-NaN)
  mean of their own group. This understates variance and is provided only as
  a sensitivity-analysis option, not as a default.
"""

from __future__ import annotations

from enum import Enum

import numpy as np


class MissingStrategy(str, Enum):
    DROP = "drop"
    IMPUTE_MEAN = "impute_mean"


def apply_missing_strategy(
    values: np.ndarray, strategy: MissingStrategy
) -> np.ndarray:
    """Return a cleaned 1-D float array according to ``strategy``.

    Raises ValueError if no valid (non-NaN) observations remain, or if
    IMPUTE_MEAN is requested but the observed mean cannot be computed.
    """
    arr = np.asarray(values, dtype=float).ravel()
    observed = arr[~np.isnan(arr)]
    if observed.size == 0:
        raise ValueError("no valid (non-NaN) observations in group")

    if strategy is MissingStrategy.DROP:
        return observed.copy()

    if strategy is MissingStrategy.IMPUTE_MEAN:
        filled = arr.copy()
        filled[np.isnan(filled)] = float(np.mean(observed))
        return filled

    raise ValueError(f"unknown missing-value strategy: {strategy!r}")


def count_missing(values: np.ndarray) -> int:
    """Number of NaN entries in the raw input."""
    arr = np.asarray(values, dtype=float).ravel()
    return int(np.isnan(arr).sum())
