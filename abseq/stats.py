"""Fixed-horizon two-sample statistics, implemented with NumPy + stdlib only.

The confidence interval is the Welch (unequal-variance) t interval. The
Student-t CDF is evaluated by deterministic trapezoidal integration of its
closed-form PDF, and quantiles are found by bisection, so results are fully
reproducible and require no SciPy dependency.
"""

from __future__ import annotations

import math

import numpy as np

_T_CDF_GRID_POINTS = 4096
_T_PPF_BISECTION_ITERS = 80
_T_PPF_BRACKET_GROWTH = 2.0


def normal_ppf(p: float) -> float:
    """Inverse standard-normal CDF (Acklam's rational approximation)."""
    if not 0.0 < p < 1.0:
        raise ValueError(f"p must be in (0, 1), got {p!r}")
    a = [
        -3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
        1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00,
    ]
    b = [
        -5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
        6.680131188771972e01, -1.328068155288572e01,
    ]
    c = [
        -7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
        -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00,
    ]
    d = [
        7.784695709041462e-03, 3.224671290700398e-01,
        2.445134137142996e00, 3.754408661907416e00,
    ]
    plow, phigh = 0.02425, 1.0 - 0.02425
    if p < plow:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
        )
    if p > phigh:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
        )
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (
        ((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0
    )


def _t_pdf(xs: np.ndarray, df: float) -> np.ndarray:
    log_norm = math.lgamma((df + 1.0) / 2.0) - math.lgamma(df / 2.0)
    log_norm -= 0.5 * math.log(df * math.pi)
    return np.exp(log_norm) * (1.0 + xs * xs / df) ** (-(df + 1.0) / 2.0)


def t_cdf(x: float, df: float) -> float:
    """Student-t CDF via trapezoidal integration of the PDF from 0 to x."""
    if df <= 0:
        raise ValueError(f"df must be positive, got {df!r}")
    if x == 0.0:
        return 0.5
    sign = 1.0 if x > 0.0 else -1.0
    grid = np.linspace(0.0, abs(x), _T_CDF_GRID_POINTS)
    area = float(np.trapezoid(_t_pdf(grid, df), grid))
    return 0.5 + sign * area


def t_ppf(p: float, df: float) -> float:
    """Student-t quantile by bisection on the integrated CDF."""
    if not 0.0 < p < 1.0:
        raise ValueError(f"p must be in (0, 1), got {p!r}")
    if p == 0.5:
        return 0.0
    if p < 0.5:
        return -t_ppf(1.0 - p, df)
    lo, hi = 0.0, 1.0
    while t_cdf(hi, df) < p:
        hi *= _T_PPF_BRACKET_GROWTH
    for _ in range(_T_PPF_BISECTION_ITERS):
        mid = 0.5 * (lo + hi)
        if t_cdf(mid, df) < p:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def welch_df(var_a: float, n_a: int, var_b: float, n_b: int) -> float:
    """Welch-Satterthwaite degrees of freedom for two sample variances."""
    term_a, term_b = var_a / n_a, var_b / n_b
    denom = term_a * term_a / (n_a - 1) + term_b * term_b / (n_b - 1)
    if denom == 0.0:
        return math.inf
    return (term_a + term_b) ** 2 / denom


def welch_t_interval(
    control: np.ndarray,
    treatment: np.ndarray,
    confidence: float,
) -> dict:
    """Welch t confidence interval for mean(treatment) - mean(control).

    Both inputs must be 1-D, finite, and have at least 2 observations.
    Returns a dict with the point estimate, standard error, degrees of
    freedom, and the two-sided interval at the given confidence level.
    """
    if not 0.0 < confidence < 1.0:
        raise ValueError(f"confidence must be in (0, 1), got {confidence!r}")
    n_c, n_t = control.size, treatment.size
    if n_c < 2 or n_t < 2:
        raise ValueError(
            f"need at least 2 observations per group, got {n_c} and {n_t}"
        )
    mean_c = float(np.mean(control))
    mean_t = float(np.mean(treatment))
    var_c = float(np.var(control, ddof=1))
    var_t = float(np.var(treatment, ddof=1))
    se = math.sqrt(var_c / n_c + var_t / n_t)
    diff = mean_t - mean_c
    df = welch_df(var_c, n_c, var_t, n_t)
    if se == 0.0:
        # Both groups are constant; the interval degenerates to a point.
        half_width = 0.0
        df_out = math.inf
    else:
        crit = t_ppf(1.0 - (1.0 - confidence) / 2.0, df)
        half_width = crit * se
        df_out = df
    return {
        "mean_control": mean_c,
        "mean_treatment": mean_t,
        "mean_difference": diff,
        "standard_error": se,
        "df": df_out,
        "confidence_level": confidence,
        "ci_lower": diff - half_width,
        "ci_upper": diff + half_width,
    }
