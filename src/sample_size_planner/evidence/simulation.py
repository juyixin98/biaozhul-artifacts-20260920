"""Simulation-based evidence -- an *independent* evaluator.

The Monte Carlo machinery here deliberately recomputes test decisions from raw
synthetic draws instead of calling the analytic kernels. Its role is to be an
external witness that a planned sample size actually delivers the promised
operating characteristic:

* normal arms: raw Gaussian draws -> test statistic -> rejection;
* binomial arms: raw binomial counts -> score statistic computed from the
  counts -> rejection; one-sample designs use an independently derived exact
  critical region.

Nothing in this module imports :mod:`sample_size_planner.estimation.solvers`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import numpy as np
from scipy.stats import binom, norm


@dataclass(frozen=True)
class SimulationResult:
    replications: int
    rejections: int
    estimated_power: float
    standard_error: float
    ci95_low: float
    ci95_high: float

    @property
    def ci95(self) -> tuple[float, float]:
        return self.ci95_low, self.ci95_high


def _summarize(rejections: int, replications: int) -> SimulationResult:
    p = rejections / replications
    se = math.sqrt(max(p * (1.0 - p), 0.0) / replications)
    # Wilson interval (reliable at extreme p near 0/1)
    z = 1.959963984540054
    denom = 1.0 + z * z / replications
    centre = (p + z * z / (2.0 * replications)) / denom
    half = z * math.sqrt(p * (1.0 - p) / replications + z * z / (4.0 * replications * replications)) / denom
    return SimulationResult(replications, int(rejections), p, se, centre - half, centre + half)


# --------------------------------------------------------------------------- #
# Independent critical-value helper (one-sample exact binomial test)
# --------------------------------------------------------------------------- #
def _exact_one_sample_thresholds(n: int, p0: float, alpha: float, two_sided: bool,
                                 greater: bool) -> tuple[Optional[int], Optional[int]]:
    """Critical region derived here, independently of the estimation package.

    Returns ``(reject_if_X_ge, reject_if_X_le)``.
    """
    tail = alpha / 2.0 if two_sided else alpha
    hi = lo = None
    if two_sided or greater:
        k = int(binom.ppf(1.0 - tail, n, p0))
        # walk to the *smallest* k with tail mass <= tail
        while binom.sf(k - 1, n, p0) > tail:
            k += 1
        while k > 0 and binom.sf(k - 2, n, p0) <= tail:
            k -= 1
        hi = max(k, 1)
    if two_sided or not greater:
        k = int(binom.ppf(tail, n, p0))
        while binom.cdf(k + 1, n, p0) <= tail:
            k += 1
        while k >= 0 and binom.cdf(k, n, p0) > tail:
            k -= 1
        lo = k
    return hi, lo


# --------------------------------------------------------------------------- #
# Normal simulation
# --------------------------------------------------------------------------- #
def simulate_normal_power(
    *,
    n0: int,
    n1: Optional[int],
    effect: float,
    sd0: float,
    sd1: float,
    alpha: float,
    two_sided: bool,
    use_t: bool,
    replications: int,
    rng: np.random.Generator,
) -> SimulationResult:
    """Estimate power from raw Gaussian samples."""
    zc = norm.isf(alpha / 2.0 if two_sided else alpha)
    if n1 is None:
        # One-sample test against a fixed reference mean (0): draw the single
        # arm under the alternative. The sample mean has variance sd0^2 / n0;
        # drawing only one N(effect, sd0) per replicate would wrongly inflate
        # the standard error by sqrt(n0).
        x = rng.normal(effect, sd0, size=(replications, n0))
        if use_t:
            stat = x.mean(axis=1) / (x.std(axis=1, ddof=1) / math.sqrt(n0))
            from scipy.stats import t as student_t
            tc = student_t.ppf(1.0 - (alpha / 2.0 if two_sided else alpha), n0 - 1)
            reject = np.abs(stat) >= tc if two_sided else stat >= tc
        else:
            stat = x.mean(axis=1) / (sd0 / math.sqrt(n0))
            reject = np.abs(stat) >= zc if two_sided else stat >= zc
    else:
        x0 = rng.normal(0.0, sd0, size=(replications, n0))
        x1 = rng.normal(effect, sd1, size=(replications, n1))
        m0, m1 = x0.mean(axis=1), x1.mean(axis=1)
        if use_t:
            v0 = x0.var(axis=1, ddof=1)
            v1 = x1.var(axis=1, ddof=1)
            sp2 = ((n0 - 1) * v0 + (n1 - 1) * v1) / (n0 + n1 - 2)
            stat = (m1 - m0) / np.sqrt(sp2 * (1.0 / n0 + 1.0 / n1))
            from scipy.stats import t as student_t
            tc = student_t.ppf(1.0 - (alpha / 2.0 if two_sided else alpha), n0 + n1 - 2)
            reject = np.abs(stat) >= tc if two_sided else stat >= tc
        else:
            stat = (m1 - m0) / np.sqrt(sd0 * sd0 / n0 + sd1 * sd1 / n1)
            reject = np.abs(stat) >= zc if two_sided else stat >= zc
    return _summarize(int(reject.sum()), replications)


# --------------------------------------------------------------------------- #
# Binomial simulation
# --------------------------------------------------------------------------- #
def simulate_binomial_power(
    *,
    n0: int,
    n1: Optional[int],
    p0: float,
    p1: float,
    alpha: float,
    two_sided: bool,
    greater: bool,
    exact_one_sample: bool,
    replications: int,
    rng: np.random.Generator,
) -> SimulationResult:
    """Estimate power from raw Binomial draws and a count-computed statistic."""
    zc = norm.isf(alpha / 2.0 if two_sided else alpha)
    if n1 is None:
        x = rng.binomial(n0, p1, size=replications)
        if exact_one_sample:
            hi, lo = _exact_one_sample_thresholds(n0, p0, alpha, two_sided, greater)
            reject = np.zeros(replications, dtype=bool)
            if hi is not None:
                reject |= x >= hi
            if lo is not None:
                reject |= x <= lo
        else:
            phat = x / n0
            z = (phat - p0) / np.sqrt(p0 * (1.0 - p0) / n0)
            reject = np.abs(z) >= zc if two_sided else (z >= zc if greater else z <= -zc)
    else:
        x0 = rng.binomial(n0, p0, size=replications)
        x1 = rng.binomial(n1, p1, size=replications)
        n_total = n0 + n1
        pbar = (x0 + x1) / n_total
        phat0 = x0 / n0
        phat1 = x1 / n1
        z = (phat1 - phat0) / np.sqrt(pbar * (1.0 - pbar) * (1.0 / n0 + 1.0 / n1))
        reject = np.abs(z) >= zc if two_sided else (z >= zc if greater else z <= -zc)
    return _summarize(int(reject.sum()), replications)
