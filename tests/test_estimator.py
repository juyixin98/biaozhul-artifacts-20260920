"""Point estimate + SE hand calculation.

Numbers below are derived by hand from chosen outcomes and within-arm
normalized weights; the estimator only recomputes them.
"""

import math

import numpy as np

from ipw_ate.contract import Estimand
from ipw_ate.estimator import estimate_ate, weighted_arm_means

# Normalized within-arm weights (each arm sums to its n=2).
W = np.array([1.6, 0.4, 0.8, 1.2])
T = np.array([1, 1, 0, 0])
Y = np.array([10.0, 20.0, 3.0, 7.0])

# mu1 = (1.6*10 + 0.4*20)/2 = 12
# mu0 = (0.8*3  + 1.2*7 )/2 = 5.4
# tau  = 6.6
EXPECTED_TAU = 6.6
# influence terms: treated w*(y-mu1)/2, control -w*(y-mu0)/2
#   [-1.6, 1.6, 0.96, -0.96] -> sum sq 6.9632
EXPECTED_SE = math.sqrt(6.9632)


def test_estimate_matches_hand_values():
    tau, se, lo, hi = estimate_ate(T, Y, W, Estimand.ATE, ci_level=0.95)
    assert isinstance(tau, float)
    assert math.isfinite(tau)
    assert math.isclose(tau, EXPECTED_TAU, rel_tol=1e-12, abs_tol=1e-12)
    assert math.isclose(se, EXPECTED_SE, rel_tol=1e-12, abs_tol=1e-10)
    assert lo < tau < hi
    # CI symmetric around estimate for the normal interval.
    assert math.isclose(hi - tau, tau - lo, rel_tol=1e-12)


def test_weighted_means_explicit():
    mu1, mu0, treated, control = weighted_arm_means(T, Y, W)
    assert math.isclose(mu1, 12.0, abs_tol=1e-12)
    assert math.isclose(mu0, 5.4, abs_tol=1e-12)
    assert treated.sum() == 2 and control.sum() == 2


def test_empty_arm_is_undefined():
    try:
        estimate_ate(np.ones(3, int), np.zeros(3), np.ones(3), Estimand.ATE)
    except ZeroDivisionError:
        return
    raise AssertionError("expected ZeroDivisionError for empty control arm")
