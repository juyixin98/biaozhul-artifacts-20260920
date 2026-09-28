"""Non-central distribution kernels.

These functions evaluate the *operating characteristic* (rejection probability
under the alternative) for explicitly parameterised tests. They contain no
search logic and no I/O, so the distribution calculations can be validated
independently of the integer search.

Conventions (all explicit in the contract types):

* direction: two-sided uses alpha/2 tails; greater/less are one-sided.
* allocation: ``n1 = round(r * n0)`` with ``r = n1 / n0``.
* binomial score test: the null variance is ``p0(1-p0)`` (pooled under H0);
  the alternative variance is unpooled.
* exact binomial power uses binomial sums only -- never a normal replacement.
"""
from __future__ import annotations

import math
from typing import Optional

import numpy as np
from scipy.stats import binom, nct, norm

# --------------------------------------------------------------------------- #
# Critical values
# --------------------------------------------------------------------------- #
def z_critical(alpha: float, two_sided: bool) -> float:
    return norm.isf(alpha / 2.0) if two_sided else norm.isf(alpha)


def t_critical(alpha: float, df: float, two_sided: bool) -> float:
    from scipy.stats import t as student_t

    return student_t.ppf(1.0 - alpha / 2.0, df) if two_sided else student_t.ppf(1.0 - alpha, df)


# --------------------------------------------------------------------------- #
# Normal (z / non-central t)
# --------------------------------------------------------------------------- #
def normal_standard_error(
    n0: int, n1: Optional[int], sd0: float, sd1: float
) -> float:
    if n1 is None:  # one-sample / one-arm against a fixed mean
        return sd0 / math.sqrt(n0)
    return math.sqrt(sd0 * sd0 / n0 + sd1 * sd1 / n1)


def normal_power(
    n0: int,
    n1: Optional[int],
    effect: float,
    sd0: float,
    sd1: float,
    alpha: float,
    two_sided: bool,
    direction_sign: float,
    use_t: bool = False,
) -> float:
    """Rejection probability under the alternative.

    ``effect`` is signed on the raw scale (mu1 - mu0); ``direction_sign`` is
    +1 for greater, -1 for less, +1 for two-sided.
    """
    se = normal_standard_error(n0, n1, sd0, sd1)
    ncf = abs(effect) / se
    if not use_t:
        if two_sided:
            zc = z_critical(alpha, True)
            return float(norm.sf(zc - ncf) + norm.cdf(-zc - ncf))
        zc = z_critical(alpha, False)
        return float(norm.sf(zc - ncf))

    df = (n0 - 1) if n1 is None else (n0 + n1 - 2)
    if two_sided:
        tc = t_critical(alpha, df, True)
        return float(nct.sf(tc, df, ncf) + nct.cdf(-tc, df, ncf))
    tc = t_critical(alpha, df, False)
    # non-centrality carries the sign for a lower-tail test
    return float(nct.sf(tc, df, ncf))


def normal_n0_continuous(
    effect: float,
    sd0: float,
    sd1: Optional[float],
    alpha: float,
    power: float,
    two_sided: bool,
    ratio: float,
    one_sample: bool,
) -> float:
    """Continuous root n0 for a z test (starting point for the integer search)."""
    zc = z_critical(alpha, two_sided)
    zb = norm.ppf(power)
    d2 = effect * effect
    if one_sample:
        return (zc + zb) ** 2 * sd0 * sd0 / d2
    s1 = sd0 if sd1 is None else sd1
    return (zc + zb) ** 2 * (sd0 * sd0 + s1 * s1 / ratio) / d2


def t_df(n0: int, n1: Optional[int]) -> int:
    return n0 - 1 if n1 is None else n0 + n1 - 2


# --------------------------------------------------------------------------- #
# Binomial -- normal (score) approximation
# --------------------------------------------------------------------------- #
def binomial_n1(n0: int, ratio: float, one_sample: bool) -> Optional[int]:
    if one_sample:
        return None
    return max(1, int(round(ratio * n0)))


def binomial_power_normal(
    n0: int,
    n1: Optional[int],
    p0: float,
    p1: float,
    alpha: float,
    two_sided: bool,
) -> float:
    """Score-test power via the normal approximation (the quantity that can
    fail at low base rates -- expected-count guard lives in the solver)."""
    zc = z_critical(alpha, two_sided)
    delta = p1 - p0
    if n1 is None:
        se_null = math.sqrt(p0 * (1.0 - p0) / n0)
        se_alt = math.sqrt(p1 * (1.0 - p1) / n0)
        cutoff = zc * se_null
    else:
        u = 1.0 / n0 + 1.0 / n1
        se_null = math.sqrt(p0 * (1.0 - p0) * u)
        se_alt = math.sqrt(p0 * (1.0 - p0) / n0 + p1 * (1.0 - p1) / n1)
        cutoff = zc * se_null
    if two_sided:
        return float(norm.sf((cutoff - delta) / se_alt) + norm.cdf((-cutoff - delta) / se_alt))
    if delta > 0:
        return float(norm.sf((cutoff - delta) / se_alt))
    return float(norm.cdf((-cutoff - delta) / se_alt))


def binomial_n0_continuous(
    p0: float,
    p1: float,
    alpha: float,
    power: float,
    two_sided: bool,
    ratio: float,
    one_sample: bool,
) -> float:
    """Classic closed form (score-test convention)."""
    zc = z_critical(alpha, two_sided)
    zb = norm.ppf(power)
    delta = p1 - p0
    q0, q1 = 1.0 - p0, 1.0 - p1
    if one_sample:
        return (zc * math.sqrt(p0 * q0) + zb * math.sqrt(p1 * q1)) ** 2 / (delta * delta)
    return (zc * math.sqrt(p0 * q0 * (1.0 + 1.0 / ratio))
            + zb * math.sqrt(p0 * q0 + p1 * q1 / ratio)) ** 2 / (delta * delta)


# --------------------------------------------------------------------------- #
# Binomial -- exact
# --------------------------------------------------------------------------- #
def _exact_one_sample_critical(n: int, p0: float, alpha: float, two_sided: bool,
                               greater: bool) -> tuple[int, Optional[int]]:
    """Critical region of the exact binomial test of H0: p = p0.

    Returns ``(k_hi, k_lo)``: reject for X >= k_hi (greater / two-sided) and
    for X <= k_lo (two-sided only). Each tail has size <= alpha (or alpha/2),
    i.e. the conservative exact test.
    """
    tail = alpha / 2.0 if two_sided else alpha
    # smallest k with P_p0(X >= k) <= tail
    k_hi = max(int(binom.ppf(1.0 - tail, n, p0)), 1)
    while binom.sf(k_hi - 1, n, p0) > tail and k_hi <= n + 1:
        k_hi += 1
    while k_hi - 1 >= 0 and binom.sf(k_hi - 2, n, p0) <= tail and k_hi > 1:
        k_hi -= 1
    k_lo: Optional[int] = None
    if two_sided:
        k_lo = min(int(binom.ppf(tail, n, p0)), n - 1)
        while binom.cdf(k_lo + 1, n, p0) <= tail and k_lo < n - 1:
            k_lo += 1
        while k_lo >= 0 and binom.cdf(k_lo, n, p0) > tail:
            k_lo -= 1
    if not greater and not two_sided:  # lower-tail only
        return n + 1, k_lo
    return k_hi, k_lo


def binomial_power_exact_one_sample(
    n: int, p0: float, p1: float, alpha: float, two_sided: bool, greater: bool
) -> float:
    if not two_sided and not greater:
        tail = alpha
        k_lo = min(int(binom.ppf(tail, n, p0)), n - 1)
        while binom.cdf(k_lo + 1, n, p0) <= tail and k_lo < n - 1:
            k_lo += 1
        while k_lo >= 0 and binom.cdf(k_lo, n, p0) > tail:
            k_lo -= 1
        return float(binom.cdf(k_lo, n, p1))

    k_hi, k_lo = _exact_one_sample_critical(n, p0, alpha, two_sided, greater)
    power = binom.sf(k_hi - 1, n, p1)
    if two_sided and k_lo is not None:
        power += binom.cdf(k_lo, n, p1)
    return float(power)


def _score_z_cutoffs_for_x0(
    x0: np.ndarray, n0: int, n1: int, zc: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """For each observed control count x0, find integer x1 cutoffs of the
    score test.

    The score statistic is
        Z = (x1/n1 - x0/n0) / sqrt(pbar(1-pbar) * (1/n0 + 1/n1)),
        pbar = (x0 + x1) / (n0 + n1).
    Setting Z^2 = zc^2 yields A x1^2 + B x1 + C = 0, giving real-valued
    roots; rejection lies outside them. Because an integer cutoff taken from a
    real root can sit on the wrong side of zc by one unit at boundary cells,
    the returned integers are *corrected against the actual score value*, so
    the acceptance set is exactly the score-test acceptance set.
    """
    N = n0 + n1
    u = 1.0 / n0 + 1.0 / n1
    k = zc * zc * u / (N * N)
    x0 = np.asarray(x0, dtype=np.float64)

    def actual_z(x1: np.ndarray) -> np.ndarray:
        pbar = (x0 + x1) / N
        var = pbar * (1.0 - pbar) * u
        diff = x1 / n1 - x0 / n0
        with np.errstate(divide="ignore", invalid="ignore"):
            return np.where(var > 0.0, diff / np.sqrt(var), 0.0)

    A = 1.0 / (n1 * n1) + k
    B = -2.0 * x0 / (n0 * n1) - k * (N - 2.0 * x0)
    C = x0 * x0 / (n0 * n0) - k * x0 * (N - x0)
    discr = B * B - 4.0 * A * C

    roots = np.full((x0.size, 2), np.nan)
    real = discr >= 0.0
    sqrt_d = np.sqrt(np.where(real, discr, 0.0))
    roots[real, 0] = (-B[real] - sqrt_d[real]) / (2.0 * A)
    roots[real, 1] = (-B[real] + sqrt_d[real]) / (2.0 * A)

    # integer cutoffs: reject low for x1 <= lo, high for x1 >= hi
    lo = np.floor(roots[:, 0])
    hi = np.ceil(roots[:, 1])

    # ---- correct the upper cutoff against the real statistic ----
    hi = np.minimum(hi, n1 + 1).astype(int)
    feasible_hi = real & (hi <= n1)
    x1_hi = np.where(feasible_hi, hi, 0).astype(float)
    z_at = actual_z(x1_hi)
    # if the rounded-up candidate still does not cross, push it up by one
    push_up = feasible_hi & (z_at < zc)
    hi[push_up] += 1
    feasible_hi = real & (hi <= n1)
    # if the integer below already crosses, pull it down by one
    x1_below = np.where(feasible_hi & (hi - 1 >= 0), hi - 1, 0).astype(float)
    z_below = actual_z(x1_below)
    pull_down = feasible_hi & (hi - 1 >= 0) & (z_below >= zc)
    hi[pull_down] -= 1

    # ---- correct the lower cutoff ----
    lo = np.maximum(lo, -1).astype(int)
    feasible_lo = real & (lo >= 0)
    x1_lo = np.where(feasible_lo, lo, 0).astype(float)
    z_lo = actual_z(x1_lo)
    pull_lo_down = feasible_lo & (z_lo > -zc)
    lo[pull_lo_down] -= 1
    feasible_lo = real & (lo >= 0)
    x1_above = np.where(feasible_lo & (lo + 1 <= n1), lo + 1, 0).astype(float)
    z_above = actual_z(x1_above)
    push_lo_up = feasible_lo & (lo + 1 <= n1) & (z_above <= -zc)
    lo[push_lo_up] += 1

    lo = np.where(real, lo, -1)
    hi = np.where(real, hi, n1 + 1)
    return lo, hi, real


def binomial_power_exact_two_sample(
    n0: int, n1: int, p0: float, p1: float, alpha: float,
    two_sided: bool, greater: bool,
) -> float:
    """Exact rejection probability of the score test by binomial summation.

    For each control count x0 (weighted by Bin(n0, p0)), the score-test
    inequality reduces to integer cutoffs on x1; the conditional rejection
    probability is then an exact Bin(n1, p1) tail. No normal approximation.

    Control counts are restricted to an +-8 sigma window; the omitted tails
    are below ~1e-15.
    """
    zc = z_critical(alpha, two_sided)
    m0, s0 = n0 * p0, math.sqrt(n0 * p0 * (1.0 - p0))
    lo_x0 = max(0, int(m0 - 8.0 * s0) - 1)
    hi_x0 = min(n0, int(m0 + 8.0 * s0) + 1)
    x0 = np.arange(lo_x0, hi_x0 + 1)
    w0 = binom.pmf(x0, n0, p0)

    cut_lo, cut_hi, real = _score_z_cutoffs_for_x0(x0, n0, n1, zc)

    if two_sided:
        pr = np.zeros_like(x0, dtype=np.float64)
        upper = cut_hi <= n1
        pr[upper] += binom.sf(cut_hi[upper].astype(int) - 1, n1, p1)
        lower = real & (cut_lo >= 0)
        pr[lower] += binom.cdf(cut_lo[lower].astype(int), n1, p1)
    elif greater:
        pr = np.where(cut_hi <= n1, binom.sf(cut_hi.astype(int) - 1, n1, p1), 0.0)
    else:
        pr = np.where(real & (cut_lo >= 0), binom.cdf(cut_lo.astype(int), n1, p1), 0.0)

    return float(np.dot(w0, pr))
