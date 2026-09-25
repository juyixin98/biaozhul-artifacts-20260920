"""Missing-value strategies for per-group observation vectors.

Non-finite values (NaN, +inf, -inf) are treated as missing. The chosen
strategy is applied independently within each group, before any statistic
is computed.
"""

from __future__ import annotations

import numpy as np

STRATEGIES = ("raise", "drop", "impute_mean")


class MissingValueError(ValueError):
    """Raised when missing values are found under the 'raise' strategy."""


def apply_missing_strategy(
    values: np.ndarray,
    strategy: str,
    group_name: str = "group",
) -> tuple[np.ndarray, int]:
    """Apply a missing-value strategy to one group's observations.

    Returns (cleaned_values, n_missing). 'impute_mean' fills missing entries
    with the mean of the observed values; note this shrinks the sample
    variance and is reported in the analysis output for transparency.
    """
    if strategy not in STRATEGIES:
        raise ValueError(
            f"unknown missing strategy {strategy!r}; expected one of {STRATEGIES}"
        )
    values = np.asarray(values, dtype=float)
    missing_mask = ~np.isfinite(values)
    n_missing = int(np.count_nonzero(missing_mask))

    if strategy == "raise":
        if n_missing > 0:
            raise MissingValueError(
                f"{group_name}: {n_missing} missing/non-finite observation(s) "
                "present and missing strategy is 'raise'"
            )
        return values, 0

    observed = values[~missing_mask]
    if strategy == "drop":
        return observed, n_missing

    # impute_mean
    if observed.size == 0:
        raise MissingValueError(
            f"{group_name}: all observations are missing; cannot impute a mean"
        )
    filled = values.copy()
    filled[missing_mask] = float(np.mean(observed))
    return filled, n_missing
