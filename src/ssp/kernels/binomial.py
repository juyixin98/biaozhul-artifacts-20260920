"""Binomial-endpoint power kernels.

Two computation routes, both explicit:

* **asymptotic** - score/normal approximation.  The one-sample statistic uses
  the null standard error ``sqrt(p0(1-p0)/n)``; the two-sample pooled statistic
  uses ``sqrt(p_bar(1-p_bar)(1/n0+1/n1))``.  Power is a non-central normal
  probability.
* **exact** - the one-sample binomial test (critical region from the exact
  Binomial CDF) and, for two samples, Fisher's conditional exact test
  (``X1 | S ~ Hypergeometric``), with unconditional power obtained by mixing
  the conditional rejection probability over ``S = X0 + X1`` under the
  alternative.

Discrete tests are conservative: the critical region is the *largest* region
whose size does not exceed alpha.  Every exact evaluation therefore also
reports the attained size.

Nothing here rounds a continuous formula into an answer: only the planning
layer's integer search decides feasibility.  Low expected counts do not trigger
a silent downgrade - the planning layer switches method and records why.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy.special import logsumexp
from scipy.stats import binom, hypergeom, norm

from ..contracts import Alternative, Allocation
from ..errors import NumericFailureError

# ---------------------------------------------------------------------------
# Expected-count diagnostics for the normal approximation
# ---------------------------------------------------------------------------


def min_expected_count(p0: float, p1: float, allocation: Allocation, two_sample: bool) -> float:
    """Smallest expected cell count (success/failure per arm)."""
    n0 = allocation.n0
    cells = [n0 * p0, n0 * (1.0 - p0)]
    if two_sample:
        n1 = allocation.n1
        cells.extend([n1 * p1, n1 * (1.0 - p1)])
    return float(min(cells))


# ---------------------------------------------------------------------------
# Asymptotic (normal) kernels
# ---------------------------------------------------------------------------


def _normal_tail_power(delta_over_se: float, alpha: float, alternative: Alternative) -> float:
    """Power of a normal score statistic.

    ``delta_over_se`` is the signed non-centrality (mean shift in SE units).
    """
    two_sided = alternative is Alternative.TWO_SIDED
    a_star = alpha / 2.0 if two_sided else alpha
    z = float(norm.ppf(1.0 - a_star))
    lam = float(delta_over_se)
    if two_sided:
        return float(norm.sf(z - lam) + norm.cdf(-z - lam))
    if alternative is Alternative.GREATER:
        return float(norm.sf(z - lam))
    return float(norm.cdf(-z - lam))


@dataclass(frozen=True)
class BinomialAsymptoticOneSample:
    p0: float
    p1: float
    alpha: float
    alternative: Alternative

    def power(self, allocation: Allocation) -> float:
        n = allocation.n0
        if n <= 0:
            return 0.0
        se0 = math.sqrt(self.p0 * (1.0 - self.p0) / n)
        return _normal_tail_power((self.p1 - self.p0) / se0, self.alpha, self.alternative)


@dataclass(frozen=True)
class BinomialAsymptoticTwoSample:
    p0: float
    p1: float
    alpha: float
    alternative: Alternative

    def power(self, allocation: Allocation) -> float:
        n0, n1 = allocation.n0, allocation.n1
        if n0 <= 0 or n1 <= 0:
            return 0.0
        total = n0 + n1
        p_bar = (n0 * self.p0 + n1 * self.p1) / total
        variance = p_bar * (1.0 - p_bar) * (1.0 / n0 + 1.0 / n1)
        if variance <= 0.0:
            return 0.0
        return _normal_tail_power(
            (self.p1 - self.p0) / math.sqrt(variance), self.alpha, self.alternative
        )


# ---------------------------------------------------------------------------
# Exact one-sample binomial test
# ---------------------------------------------------------------------------


def _exact_one_sample_critical(n: int, p0: float, alpha: float, alternative: Alternative) -> tuple:
    """Return ``(reject_predicate description, c_low, c_high, attained_size)``.

    Rejection is ``X <= c_low`` and/or ``X >= c_high``.  Unused bounds are
    marked with ``-1`` / ``n + 1``.
    """
    if alternative is Alternative.TWO_SIDED:
        tail = alpha / 2.0
        c_low = _lower_boundary(n, p0, tail)
        c_high = _upper_boundary(n, p0, tail)
        size = (binom.cdf(c_low, n, p0) if c_low >= 0 else 0.0) + (
            binom.sf(c_high - 1, n, p0) if c_high <= n else 0.0
        )
        return c_low, c_high, float(size)
    tail = alpha
    if alternative is Alternative.GREATER:
        c_high = _upper_boundary(n, p0, tail)
        size = binom.sf(c_high - 1, n, p0) if c_high <= n else 0.0
        return -1, c_high, float(size)
    c_low = _lower_boundary(n, p0, tail)
    size = binom.cdf(c_low, n, p0) if c_low >= 0 else 0.0
    return c_low, n + 1, float(size)


def _upper_boundary(n: int, p0: float, tail_alpha: float) -> int:
    """Smallest c with P_p0(X >= c) <= tail_alpha."""
    c = int(binom.ppf(1.0 - tail_alpha, n, p0)) + 1
    # Correct ppf conservatism/ties by explicit PMF checks.
    while c > 0 and binom.sf(c - 2, n, p0) <= tail_alpha:
        c -= 1
    while c <= n and binom.sf(c - 1, n, p0) > tail_alpha:
        c += 1
    return c


def _lower_boundary(n: int, p0: float, tail_alpha: float) -> int:
    """Largest c with P_p0(X <= c) <= tail_alpha; -1 if no mass can be used."""
    c = int(binom.ppf(tail_alpha, n, p0))
    while c >= 0 and binom.cdf(c, n, p0) > tail_alpha:
        c -= 1
    while c + 1 <= n and binom.cdf(c + 1, n, p0) <= tail_alpha:
        c += 1
    return c


@dataclass(frozen=True)
class BinomialExactOneSample:
    p0: float
    p1: float
    alpha: float
    alternative: Alternative

    def critical_region(self, allocation: Allocation) -> tuple[int, int, float]:
        return _exact_one_sample_critical(allocation.n0, self.p0, self.alpha, self.alternative)

    def power(self, allocation: Allocation) -> float:
        n = allocation.n0
        if n <= 0:
            return 0.0
        c_low, c_high, _ = self.critical_region(allocation)
        power = 0.0
        if c_low >= 0:
            power += float(binom.cdf(c_low, n, self.p1))
        if c_high <= n:
            power += float(binom.sf(c_high - 1, n, self.p1))
        return float(min(1.0, power))


# ---------------------------------------------------------------------------
# Exact two-sample Fisher test with unconditional power
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BinomialExactTwoSample:
    p0: float
    p1: float
    alpha: float
    alternative: Alternative

    def power(self, allocation: Allocation) -> float:
        n0, n1 = allocation.n0, allocation.n1
        if n0 <= 0 or n1 <= 0:
            return 0.0
        total = n0 + n1
        two_sided = self.alternative is Alternative.TWO_SIDED
        tail = self.alpha / 2.0 if two_sided else self.alpha

        # Independent-binomial log PMFs under the alternative.  Only the part
        # of the support with non-negligible mass is visited: under low base
        # rates S = X0+X1 concentrates on a handful of values, which makes the
        # exact unconditional power tractable exactly where the approximation
        # fails.  Threshold 1e-30 per outcome keeps truncation error negligible.
        x0_full = np.arange(n0 + 1, dtype=np.float64)
        x1_full = np.arange(n1 + 1, dtype=np.float64)
        log_p0_full = binom.logpmf(x0_full, n0, self.p0)
        log_p1_full = binom.logpmf(x1_full, n1, self.p1)
        keep0 = np.nonzero(log_p0_full > -70.0)[0]
        keep1 = np.nonzero(log_p1_full > -70.0)[0]
        if keep0.size == 0 or keep1.size == 0:
            return 0.0
        x0_idx = keep0.astype(np.int64)
        x1_idx = keep1.astype(np.int64)

        s_min = int(x0_idx[0] + x1_idx[0])
        s_max = int(x0_idx[-1] + x1_idx[-1])

        power = 0.0
        for s in range(s_min, s_max + 1):
            lo = max(int(x1_idx[0]), s - int(x0_idx[-1]))
            hi = min(int(x1_idx[-1]), s - int(x0_idx[0]))
            if lo > hi:
                continue
            x1_vals = np.arange(lo, hi + 1)
            x0_vals = s - x1_vals
            # Conditional weights of X1 given S=s under p0 != p1.
            log_joint = log_p1_full[x1_vals] + log_p0_full[x0_vals]
            log_denom = logsumexp(log_joint)
            if not math.isfinite(log_denom) or log_denom == -math.inf:
                continue  # P(S=s) is zero at machine precision.

            reject = self._rejection_mask(s, x1_vals, n0, n1, total, tail)
            if not reject.any():
                continue
            cond_reject = float(np.exp(logsumexp(log_joint[reject]) - log_denom))
            p_s = float(np.exp(log_denom))
            power += p_s * cond_reject
        power = min(1.0, max(0.0, power))
        if not math.isfinite(power):  # pragma: no cover - defensive boundary
            raise NumericFailureError("Fisher exact power evaluation produced non-finite result")
        return power

    def _rejection_mask(
        self, s: int, x1_vals: np.ndarray, n0: int, n1: int, total: int, tail: float
    ) -> np.ndarray:
        """Conditional Fisher rejection set for fixed table margin ``s``."""
        # P(X1 >= x | S=s) under the common-p null.
        sf = hypergeom.sf(x1_vals - 1, total, n1, s)
        cdf = hypergeom.cdf(x1_vals, total, n1, s)
        if self.alternative is Alternative.TWO_SIDED:
            return (sf <= tail) | (cdf <= tail)
        if self.alternative is Alternative.GREATER:
            return sf <= tail
        return cdf <= tail


# ---------------------------------------------------------------------------
# Continuous starting estimates (asymptotic; only seed the integer search)
# ---------------------------------------------------------------------------


def continuous_n0_one_sample(
    p0: float, p1: float, alpha: float, target_power: float, alternative: Alternative
) -> float:
    """Classical two-variance formula: null variance at the boundary,
    alternative variance for the power shift."""
    a_star = alpha / 2.0 if alternative is Alternative.TWO_SIDED else alpha
    z_a = float(norm.ppf(1.0 - a_star))
    z_b = float(norm.ppf(target_power))
    num = (z_a * math.sqrt(p0 * (1.0 - p0)) + z_b * math.sqrt(p1 * (1.0 - p1))) ** 2
    return num / (p1 - p0) ** 2


def continuous_n0_two_sample(
    p0: float,
    p1: float,
    alpha: float,
    target_power: float,
    alternative: Alternative,
    allocation_ratio: float,
) -> float:
    """Fleiss-style unrounded n0 with arm ratio r = n1/n0."""
    r = allocation_ratio
    a_star = alpha / 2.0 if alternative is Alternative.TWO_SIDED else alpha
    z_a = float(norm.ppf(1.0 - a_star))
    z_b = float(norm.ppf(target_power))
    p_bar = (p0 + r * p1) / (1.0 + r)
    null_term = z_a * math.sqrt(p_bar * (1.0 - p_bar) * (1.0 + 1.0 / r))
    alt_term = z_b * math.sqrt(p1 * (1.0 - p1) + p0 * (1.0 - p0) / r)
    return (null_term + alt_term) ** 2 / (p1 - p0) ** 2
