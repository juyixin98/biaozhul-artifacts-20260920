"""Normal-endpoint power kernels.

Two sampling models, both explicit:

* ``known_sigma=True``  -> standard normal (z) statistic.
* ``known_sigma=False`` -> Student t statistic with a **non-central t** law
                           under the alternative (SciPy ``nct``).

Two-sample tests use the pooled-variance Student t (equal-variance assumption,
stated in the contract).  All formulas work for the one-sample and two-sample
cases, all three directions, and an arbitrary arm allocation ratio ``r``.

The non-central machinery is independently validated in
:func:`validate_noncentral_t` (central-case size calibration and large-df
convergence to the normal kernel); the test suite asserts on its outputs.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from scipy.stats import nct, norm, t

from ..contracts import Alternative, Allocation
from ..errors import NoncentralityError

# ---------------------------------------------------------------------------
# Allocation
# ---------------------------------------------------------------------------


def allocate(n0: int, allocation_ratio: float) -> Allocation:
    """Integer arm split: ``n1`` is the rounded ratio, at least 1."""
    n1 = max(1, int(math.floor(float(allocation_ratio) * n0 + 0.5)))
    return Allocation(n0=int(n0), n1=int(n1))


def total_minus_one(allocation: Allocation) -> Allocation:
    """Remove one subject from the larger arm (ties -> arm 1)."""
    n0, n1 = allocation.n0, allocation.n1
    if n1 >= n0 and n1 > 0:
        return Allocation(n0=n0, n1=n1 - 1)
    return Allocation(n0=n0 - 1, n1=n1)


# ---------------------------------------------------------------------------
# Non-centrality parameters
# ---------------------------------------------------------------------------


def _noncentrality_one(d_abs: float, n: int) -> float:
    return d_abs * math.sqrt(n)


def _noncentrality_two(d_abs: float, n0: int, n1: int) -> float:
    # SE(mu1_hat - mu0_hat) = sigma * sqrt(1/n0 + 1/n1)
    return d_abs * math.sqrt(n0 * n1 / (n0 + n1))


def _nct_cdf(x: float, df: float, nc: float) -> float:
    """CDF of the non-central t with explicit failure handling.

    SciPy's ``nct.cdf`` returns NaN in some far-tail regions (negative x with
    positive nc, large df) even though the probability is well-defined.  The
    symmetry identity ``F_t(x; nc) = 1 - F_t(-x; -nc)`` evaluates the mirrored
    tail via ``sf``, which is stable there.  A mirrored tail below machine
    resolution is effectively zero, never NaN-as-success.
    """
    if not math.isfinite(df) or df <= 0.0:
        raise NoncentralityError(
            "non-central t requires positive degrees of freedom",
            details={"x": x, "df": df, "nc": nc},
        )
    try:
        value = float(nct.cdf(x, df, nc))
    except Exception as exc:
        raise NoncentralityError(
            "non-central t CDF evaluation failed",
            details={"x": x, "df": df, "nc": nc, "scipy_error": str(exc)},
        ) from exc
    if math.isfinite(value):
        return value
    try:
        mirrored = float(nct.sf(-x, df, -nc))
    except Exception as exc:
        raise NoncentralityError(
            "non-central t CDF and mirrored sf both failed",
            details={"x": x, "df": df, "nc": nc, "scipy_error": str(exc)},
        ) from exc
    if not math.isfinite(mirrored):
        # Genuine underflow region: probability mass below ~1e-308.
        return 0.0
    return mirrored


def _power_from_standard(
    cdf_lower,
    crit_two_sided: float,
    lam: float,
    two_sided: bool,
) -> float:
    """Power of a symmetric-location test from its lower-tail CDF.

    The statistic has non-centrality ``+lam`` (alternative above the null).
    """
    if two_sided:
        upper = 1.0 - cdf_lower(crit_two_sided, lam)
        lower = cdf_lower(-crit_two_sided, lam)
        return float(min(1.0, max(0.0, upper + lower)))
    return float(min(1.0, max(0.0, 1.0 - cdf_lower(crit_two_sided, lam))))


# ---------------------------------------------------------------------------
# Power evaluations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NormalModel:
    """Bound normal model parameters for repeated power evaluation."""

    d_abs: float
    alpha: float
    alternative: Alternative
    two_sample: bool
    known_sigma: bool

    @property
    def two_sided(self) -> bool:
        return self.alternative is Alternative.TWO_SIDED

    @property
    def _critical_prob(self) -> float:
        return 1.0 - (self.alpha / 2.0 if self.two_sided else self.alpha)

    def power(self, allocation: Allocation) -> float:
        n0 = allocation.n0
        n1 = allocation.n1 if self.two_sample else None
        if n0 <= 0 or (self.two_sample and (n1 is None or n1 <= 0)):
            return 0.0
        if self.two_sample and not self.known_sigma and n0 + n1 <= 2:
            return 0.0  # pooled variance needs df = N - 2 > 0
        if not self.two_sample and not self.known_sigma and n0 <= 1:
            return 0.0

        crit_p = self._critical_prob
        if self.two_sample:
            lam = _noncentrality_two(self.d_abs, n0, n1)
        else:
            lam = _noncentrality_one(self.d_abs, n0)

        if self.known_sigma:
            zcrit = float(norm.ppf(crit_p))

            def cdf_lower(x: float, _lam: float) -> float:
                return float(norm.cdf(x - _lam))
        else:
            df = (n0 + n1 - 2) if self.two_sample else (n0 - 1)
            tcrit = float(t.ppf(crit_p, df))

            def cdf_lower(x: float, _lam: float) -> float:
                return _nct_cdf(x, df, _lam)

            zcrit = tcrit
        return _power_from_standard(cdf_lower, zcrit, lam, self.two_sided)


# ---------------------------------------------------------------------------
# Continuous starting estimate (z-based analytic formula)
# ---------------------------------------------------------------------------


def continuous_n0(
    d_abs: float,
    alpha: float,
    target_power: float,
    alternative: Alternative,
    two_sample: bool,
    allocation_ratio: float,
) -> float:
    """Analytic unrounded solution.

    One sample: ``n = (z_{1-a*} + z_{1-b})^2 / d^2``.

    Two samples with ``n1/n0 = r``:
    ``n0 = (zsum/d)^2 (1 + 1/r)``; total ``N = (zsum/d)^2 (1+r)^2/r``.

    ``a*`` is alpha for one-sided tests and alpha/2 for two-sided.
    """
    a_star = alpha / 2.0 if alternative is Alternative.TWO_SIDED else alpha
    zsum = float(norm.ppf(1.0 - a_star)) + float(norm.ppf(target_power))
    base = (zsum / d_abs) ** 2
    if not two_sample:
        return base
    return base * (1.0 + 1.0 / allocation_ratio)


# ---------------------------------------------------------------------------
# Independent self-check of the non-central distribution machinery
# ---------------------------------------------------------------------------


def validate_noncentral_t() -> dict:
    """Numerical self-check, run at startup and asserted on in the tests.

    1. Under nc=0 the non-central t is the central t (correct test size).
    2. With many df, non-central t power collapses to normal power.
    """
    size_checks = []
    for df in (3, 10, 30, 100):
        for p in (0.025, 0.05, 0.95, 0.975):
            crit = float(t.ppf(p, df))
            got = _nct_cdf(crit, df, 0.0)
            size_checks.append({"df": df, "p": p, "cdf": got, "abs_error": abs(got - p)})

    d = 0.5
    alpha = 0.05
    model_t = NormalModel(
        d_abs=d, alpha=alpha, alternative=Alternative.TWO_SIDED,
        two_sample=False, known_sigma=False,
    )
    model_z = NormalModel(
        d_abs=d, alpha=alpha, alternative=Alternative.TWO_SIDED,
        two_sample=False, known_sigma=True,
    )
    convergence = []
    for n in (50, 100, 200, 500):
        alloc = Allocation(n0=n, n1=1)
        convergence.append(
            {
                "n": n,
                "t_power": model_t.power(alloc),
                "z_power": model_z.power(alloc),
                "abs_gap": abs(model_t.power(alloc) - model_z.power(alloc)),
            }
        )
    return {"size_checks": size_checks, "large_df_convergence": convergence}
