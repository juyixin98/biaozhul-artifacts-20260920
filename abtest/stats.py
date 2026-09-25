"""Welch two-sample analysis: difference of means with a fixed-confidence
interval, for a pre-registered, fixed sample size.

VALIDITY BOUNDARY (read before use):
    The interval is valid only when the sample size and the confidence level
    were fixed *before* looking at the data. Repeatedly computing this
    interval as data accrues and stopping when it excludes zero ("peeking")
    inflates the false-positive rate beyond the nominal alpha. This tool
    does NOT support sequential monitoring or optional stopping.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import numpy as np

from .tdist import t_ppf

MIN_GROUP_SIZE = 2  # need at least 2 observations to estimate a variance


@dataclass(frozen=True)
class AnalysisResult:
    """Result of a fixed-sample A/B mean-difference analysis."""

    mean_control: float
    mean_treatment: float
    mean_diff: float  # treatment - control
    ci_lower: float
    ci_upper: float
    confidence_level: float
    n_control: int
    n_treatment: int
    std_control: float
    std_treatment: float
    std_error: float
    degrees_of_freedom: float  # Welch–Satterthwaite
    missing_strategy: str
    n_missing_control: int
    n_missing_treatment: int

    def to_dict(self) -> dict:
        return asdict(self)


def welch_degrees_of_freedom(
    var_c: float, n_c: int, var_t: float, n_t: int
) -> float:
    """Welch–Satterthwaite degrees of freedom."""
    se_c = var_c / n_c
    se_t = var_t / n_t
    total = se_c + se_t
    if total == 0.0:
        # Both groups are constant: the difference is exact, df is irrelevant.
        # Return a large df so the t quantile approaches the normal one.
        return 1.0e6
    denom = (se_c * se_c) / (n_c - 1) + (se_t * se_t) / (n_t - 1)
    return (total * total) / denom


def welch_mean_diff_ci(
    control: np.ndarray,
    treatment: np.ndarray,
    confidence_level: float = 0.95,
    missing_strategy: str = "drop",
) -> AnalysisResult:
    """Welch confidence interval for mean(treatment) - mean(control).

    Parameters
    ----------
    control, treatment:
        1-D observations per group; may contain NaN (handled per
        ``missing_strategy``).
    confidence_level:
        Pre-registered confidence level, e.g. 0.95. Must be in (0, 1).
    missing_strategy:
        "drop" (default) or "impute_mean". See :mod:`abtest.missing`.

    Returns
    -------
    AnalysisResult
        Mean difference and its two-sided confidence interval.

    Notes
    -----
    Valid only for a fixed, pre-registered sample size. Not valid under
    sequential peeking / optional stopping.
    """
    from .missing import MissingStrategy, apply_missing_strategy, count_missing

    if not 0.0 < confidence_level < 1.0:
        raise ValueError(
            f"confidence_level must be in (0, 1), got {confidence_level}"
        )
    strategy = MissingStrategy(missing_strategy)

    n_missing_c = count_missing(control)
    n_missing_t = count_missing(treatment)
    c = apply_missing_strategy(control, strategy)
    t = apply_missing_strategy(treatment, strategy)

    n_c, n_t = c.size, t.size
    if n_c < MIN_GROUP_SIZE or n_t < MIN_GROUP_SIZE:
        raise ValueError(
            "each group needs at least "
            f"{MIN_GROUP_SIZE} valid observations after missing-value "
            f"handling, got control={n_c}, treatment={n_t}"
        )

    mean_c = float(np.mean(c))
    mean_t = float(np.mean(t))
    var_c = float(np.var(c, ddof=1))
    var_t = float(np.var(t, ddof=1))

    std_error = math.sqrt(var_c / n_c + var_t / n_t)
    df = welch_degrees_of_freedom(var_c, n_c, var_t, n_t)
    alpha = 1.0 - confidence_level
    crit = t_ppf(1.0 - alpha / 2.0, df)
    diff = mean_t - mean_c
    half_width = crit * std_error

    return AnalysisResult(
        mean_control=mean_c,
        mean_treatment=mean_t,
        mean_diff=diff,
        ci_lower=diff - half_width,
        ci_upper=diff + half_width,
        confidence_level=confidence_level,
        n_control=n_c,
        n_treatment=n_t,
        std_control=math.sqrt(var_c),
        std_treatment=math.sqrt(var_t),
        std_error=std_error,
        degrees_of_freedom=df,
        missing_strategy=strategy.value,
        n_missing_control=n_missing_c,
        n_missing_treatment=n_missing_t,
    )
