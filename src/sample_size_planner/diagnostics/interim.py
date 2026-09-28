"""Interim ("multiple look") handling under an *explicit fixed commitment*.

The statistical boundary this module enforces: peeking at the data changes the
type-I error, so interim looks are legal only when

1. the number of looks is fixed in advance (capped, never open-ended), and
2. each look uses a pre-specified alpha-spending boundary whose overall size
   is exactly alpha.

Anything else -- an ad-hoc extra look, a look beyond the committed schedule --
is rejected with the explicit failure category
``INTERIM_LOOK_OUTSIDE_COMMITMENT`` rather than silently reusing the fixed-N
critical value. A repeated-significance ("test every week, stop when
significant") design is *not* a fixed-sample commitment and is not reported as
one.

Canonical special cases used as independent analytic witnesses:

* a single committed look (K=1) reduces to the ordinary critical value
  ``z_{1-alpha}``;
* a Pocock boundary is constant on the standardised score scale; with K=2 and
  alpha=0.05 one-sided the constant is z_P ~ 2.141 (a hand-derived reference
  asserted in the tests).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import List, Sequence, Tuple

import numpy as np
from scipy.optimize import brentq
from scipy.stats import multivariate_normal, norm

from ..contracts import FailureCategory

# Tolerances: 1e-6 absolute error on a crossing probability is four orders
# smaller than the alpha values used, and keeps the adaptive MVN routine fast
# even when a trial boundary puts the orthant in an extreme tail.
_CDF_KWARGS = dict(allow_singular=True, abseps=1e-7, releps=1e-6, maxpts=200_000)


class BoundaryFamily(str, Enum):
    OBRIEN_FLEMING = "obrien_fleming"
    POCOCK = "pocock"
    FIXED = "fixed"  # no interim analysis; single final look


class InterimError(ValueError):
    """Validation failure carrying an explicit FailureCategory."""

    def __init__(self, category: FailureCategory, message: str):
        super().__init__(message)
        self.category = category


@dataclass(frozen=True)
class LookSchedule:
    """Fixed-in-advance information fractions and the boundaries on them."""

    information_times: Tuple[float, ...]       # strictly increasing, ends at 1
    z_boundaries: Tuple[float, ...]            # per-look critical values
    family: BoundaryFamily
    alpha: float
    two_sided: bool

    @property
    def n_looks(self) -> int:
        return len(self.information_times)


@dataclass(frozen=True)
class InterimPlan:
    schedule: LookSchedule
    cumulative_alpha_spent: Tuple[float, ...]
    per_look_nominal_alpha: Tuple[float, ...]
    alternative_power: float
    fixed_sample_power: float

    @property
    def power_loss_vs_fixed(self) -> float:
        return max(0.0, self.fixed_sample_power - self.alternative_power)


# --------------------------------------------------------------------------- #
# Multivariate-normal rectangle integrals
# --------------------------------------------------------------------------- #
def _upper_orthant_cdf(cov: np.ndarray, mean: np.ndarray, upper: np.ndarray) -> float:
    """P(X_1 <= u_1, ..., X_k <= u_k) for X ~ N(mean, cov).

    Standardising to a correlation matrix keeps the Fortran MVN routine well
    conditioned. This is the single primitive the first-crossing probability
    needs; no 2^k inclusion-exclusion is required (see
    :func:`_first_cross_probability`).
    """
    std = np.sqrt(np.diag(cov))
    corr = cov / np.outer(std, std)
    u = (upper - mean) / std
    return float(multivariate_normal.cdf(u, mean=np.zeros(len(u)), cov=corr,
                                         **_CDF_KWARGS))


def _cov_btimes(times: np.ndarray) -> np.ndarray:
    """Covariance of the Brownian statistic B(t_k): min(t_i, t_j)."""
    return np.minimum.outer(times, times)


def _first_cross_probability(boundary_z: np.ndarray, times: np.ndarray,
                             drift: float) -> float:
    """Probability of ever crossing the upper boundary.

    Under drift ``mu`` the statistic B(t_k) is jointly normal with
    mean ``mu * t_k`` (Brownian drift parameterisation) and covariance
    min(t_i, t_j); the Z-scale boundary ``b_k`` becomes the B-scale boundary
    ``b_k * sqrt(t_k)``.

    The probability that the *first* crossing occurs at look j is the
    difference of two upper-orthant probabilities:

        P(B_1 < b_1, ..., B_{j-1} < b_{j-1})
      - P(B_1 < b_1, ..., B_{j-1} < b_{j-1}, B_j < b_j)

    so the whole crossing probability telescopes cheaply (two MVN CDF calls
    per look), which is effectively exact and far faster than a 2^k
    inclusion-exclusion box integral.
    """
    t = np.asarray(times, dtype=float)
    b_brownian = boundary_z * np.sqrt(t)
    means0 = drift * t
    # The crossing probability is 1 - P(survive every look); with nested
    # prefixes the orthant CDFs are computed in a single increasing sweep, so
    # exactly k MVN calls total.
    return float(
        1.0
        - _upper_orthant_cdf(_cov_btimes(t), means0, b_brownian)
    )


# --------------------------------------------------------------------------- #
# Boundary calibration
# --------------------------------------------------------------------------- #
def _overall_size(boundary_z: np.ndarray, times: np.ndarray) -> float:
    return _first_cross_probability(boundary_z, times, drift=0.0)


def _calibrate(shape: callable, times: np.ndarray, alpha_one_side: float) -> np.ndarray:
    """Find scale c with overall crossing probability under H0 = alpha.

    The bracket is anchored on the fixed-sample critical value ``z_{1-a}``:
    any repeated-look boundary constant lies near it (slightly above), which
    avoids evaluating the MVN integral at extreme, slow tails.
    """
    anchor = float(norm.isf(alpha_one_side))

    def size_at(c: float) -> float:
        return _overall_size(shape(c), times)

    lo, hi = 0.6 * anchor, 2.5 * anchor
    f_lo, f_hi = size_at(lo) - alpha_one_side, size_at(hi) - alpha_one_side
    while f_lo * f_hi > 0:
        lo *= 0.5 if f_lo < 0 else 1.0
        hi *= 1.4 if f_hi > 0 else 1.0
        f_lo, f_hi = size_at(lo) - alpha_one_side, size_at(hi) - alpha_one_side
        if lo < 0.05 or hi > 50.0:
            raise RuntimeError("boundary calibration failed to bracket alpha")

    def f(c: float) -> float:
        return size_at(c) - alpha_one_side

    c_star = brentq(f, lo, hi, xtol=1e-7, rtol=1e-7)
    return shape(c_star)


def obrien_fleming_boundaries(times: Sequence[float], alpha: float, two_sided: bool) -> np.ndarray:
    """Classical O-F: boundary on the Z scale is c / sqrt(t_k) (very stringent
    early, converging to the fixed critical value at the final look)."""
    t = np.asarray(times, dtype=float)
    a = alpha / 2.0 if two_sided else alpha
    return _calibrate(lambda c: c / np.sqrt(t), t, a)


def pocock_boundaries(times: Sequence[float], alpha: float, two_sided: bool) -> np.ndarray:
    """Pocock: a constant boundary on the standardised Z scale at every look."""
    t = np.asarray(times, dtype=float)
    a = alpha / 2.0 if two_sided else alpha
    return _calibrate(lambda c: np.full_like(t, c), t, a)


# --------------------------------------------------------------------------- #
# Commitment validation
# --------------------------------------------------------------------------- #
def build_schedule(
    information_times: Sequence[float],
    *,
    alpha: float,
    two_sided: bool,
    family: BoundaryFamily = BoundaryFamily.OBRIEN_FLEMING,
    max_looks: int = 20,
) -> LookSchedule:
    """Validate the commitment and return calibrated boundaries.

    Fails explicitly (never by silently repairing the input) when:
    * times are not strictly increasing in (0, 1];
    * the schedule omits the final analysis at t = 1;
    * the number of looks exceeds the configured cap.
    """
    t = [float(x) for x in information_times]
    if not t:
        raise InterimError(FailureCategory.INVALID_INPUT, "empty look schedule")
    if any(x <= 0.0 or x > 1.0 for x in t):
        raise InterimError(FailureCategory.INVALID_INPUT,
                           f"information times must lie in (0, 1], got {t}")
    if any(b <= a for a, b in zip(t, t[1:])):
        raise InterimError(FailureCategory.INVALID_INPUT,
                           f"information times must be strictly increasing, got {t}")
    if abs(t[-1] - 1.0) > 1e-9:
        raise InterimError(FailureCategory.INTERIM_LOOK_OUTSIDE_COMMITMENT,
                           f"schedule must include the final look at t=1; got {t[-1]}")
    if len(t) > max_looks:
        raise InterimError(FailureCategory.TOO_MANY_INTERIM_LOOKS,
                           f"{len(t)} looks exceed the configured maximum {max_looks}")

    t_arr = np.asarray(t)
    if family is BoundaryFamily.FIXED or len(t) == 1:
        zc = norm.isf(alpha / 2.0 if two_sided else alpha)
        return LookSchedule((t[0],), (float(zc),), BoundaryFamily.FIXED, alpha, two_sided)
    if family is BoundaryFamily.OBRIEN_FLEMING:
        b = obrien_fleming_boundaries(t_arr, alpha, two_sided)
    elif family is BoundaryFamily.POCOCK:
        b = pocock_boundaries(t_arr, alpha, two_sided)
    else:  # pragma: no cover - closed enum
        raise InterimError(FailureCategory.INVALID_INPUT, f"unknown family {family}")
    return LookSchedule(tuple(t), tuple(float(x) for x in b), family, alpha, two_sided)


def verify_look_is_committed(schedule: LookSchedule, requested_time: float) -> None:
    """Guard against an unplanned peek at run time."""
    if any(abs(requested_time - x) <= 1e-9 for x in schedule.information_times):
        return
    raise InterimError(
        FailureCategory.INTERIM_LOOK_OUTSIDE_COMMITMENT,
        f"look at information time {requested_time} is not part of the committed "
        f"schedule {schedule.information_times}; an unplanned look inflates type-I error",
    )


# --------------------------------------------------------------------------- #
# Full plan: boundaries, alpha accounting, alternative-side power
# --------------------------------------------------------------------------- #
def plan_interim(
    information_times: Sequence[float],
    *,
    alpha: float,
    drift_at_full_information: float,
    two_sided: bool,
    family: BoundaryFamily = BoundaryFamily.OBRIEN_FLEMING,
    max_looks: int = 20,
) -> InterimPlan:
    """Calibrated boundaries plus size accounting and alternative-side power.

    ``drift_at_full_information`` is the non-centrality (Z-scale mean) the
    fixed-N design would have at the committed total N; it lets the caller see
    how much power the interim option costs at the same total sample size.
    """
    schedule = build_schedule(information_times, alpha=alpha, two_sided=two_sided,
                              family=family, max_looks=max_looks)
    b = np.asarray(schedule.z_boundaries)
    t = np.asarray(schedule.information_times)
    one_side_alpha = alpha / 2.0 if two_sided else alpha

    # Cumulative alpha spent at each look = first-cross probability up to k.
    spent: List[float] = []
    for j in range(len(t)):
        spent.append(_first_cross_probability(b[:j + 1], t[:j + 1], drift=0.0))
    nominal = tuple(float(norm.sf(x)) for x in b)

    # Drift parameter: Z(t) has mean lambda*sqrt(t); Brownian B = sqrt(t) Z
    # then has mean lambda*t, i.e. drift lambda.
    alt_power = _first_cross_probability(b, t, drift=drift_at_full_information)
    zc = norm.isf(one_side_alpha)
    fixed_power = float(norm.sf(zc - drift_at_full_information))
    return InterimPlan(
        schedule=schedule,
        cumulative_alpha_spent=tuple(spent),
        per_look_nominal_alpha=nominal,
        alternative_power=alt_power,
        fixed_sample_power=fixed_power,
    )
