"""Hand-computed weight / ESS checks.

Reference values are computed by hand below from raw fractions -- never
produced by the estimator under test.
"""

import math

import numpy as np
import pytest

from ipw_ate.contract import Estimand, IPWConfig
from ipw_ate.weights import build_weights, effective_sample_size

# e = [.2, .8 | .4, .6], t = [1,1,0,0]; trim .01/.99 does not bind.
PROP = np.array([0.2, 0.8, 0.4, 0.6])
T = np.array([1, 1, 0, 0])

# Raw ATE weights:
#   treated: 1/.2 = 5.00, 1/.8 = 1.25          (sum 6.25)
#   control: 1/.6 = 1.6667, 1/.4 = 2.5         (sum 4.1667)
# Hajek factor n_arm/sum_arm: treated 2/6.25 = .32, control 2/4.1667 = .48
EXPECTED_ATE_W = np.array([5.0 * 0.32, 1.25 * 0.32,
                           (5.0 / 3.0) * 0.48, 2.5 * 0.48])


def test_ate_weights_match_hand_calculation():
    cfg = IPWConfig(estimand=Estimand.ATE)
    w = build_weights(T, PROP, cfg)
    assert w.shape == (4,)
    np.testing.assert_allclose(w, EXPECTED_ATE_W, atol=1e-12)
    # Each arm normalizes to its own count.
    assert math.isclose(w[T == 1].sum(), 2.0, rel_tol=1e-12)
    assert math.isclose(w[T == 0].sum(), 2.0, rel_tol=1e-12)


def test_effective_sample_size_matches_hand_fractions():
    # Treated raw weights 5, 1.25: 6.25^2 / (25 + 1.5625) = 1.470588...
    assert math.isclose(effective_sample_size(np.array([5.0, 1.25])),
                        6.25 ** 2 / 26.5625, rel_tol=1e-12)
    # Control 5/3, 2.5: (25/6)^2 / (25/9 + 25/4) = 1.92307...
    raw0 = np.array([5.0 / 3.0, 2.5])
    expected = raw0.sum() ** 2 / np.sum(raw0 ** 2)
    assert math.isclose(effective_sample_size(raw0), expected, rel_tol=1e-12)
    assert effective_sample_size(np.array([])) == 0.0
    assert effective_sample_size(np.array([0.0, 0.0])) == 0.0


def test_fixed_truncation_caps_extreme_weight():
    # Score .005 would give weight 200; fixed trim at .01 caps it at 100.
    e = np.array([0.005, 0.5])
    t = np.array([1, 1])
    cfg = IPWConfig(estimand=Estimand.ATE, n_splits=2)
    w = build_weights(t, e, cfg)
    # raw clipped weights [100, 2], factor 2/102
    np.testing.assert_allclose(w, [100 * 2 / 102, 2 * 2 / 102], atol=1e-12)
    assert cfg.trim_version == "fixed_ps_0.01_0.99_v1"


def test_att_weights_target_treated_population():
    # Treated get weight 1; controls reweighted by odds e/(1-e).
    e = np.array([0.7, 0.9, 0.4, 0.6])
    t = np.array([1, 1, 0, 0])
    cfg = IPWConfig(estimand=Estimand.ATT, n_splits=2)
    w = build_weights(t, e, cfg)
    np.testing.assert_allclose(w[t == 1], [1.0, 1.0])
    raw0 = np.array([0.4 / 0.6, 0.6 / 0.4])  # 2/3, 3/2
    np.testing.assert_allclose(w[t == 0], raw0 * (2 / raw0.sum()), atol=1e-12)


def test_boundary_propensity_is_undefined_not_replaced():
    cfg = IPWConfig(estimand=Estimand.ATE, n_splits=2)
    for bad in (0.0, 1.0):
        e = np.array([bad, 0.5])
        t = np.array([1, 0])
        with pytest.raises(Exception) as exc:
            build_weights(t, e, cfg)
        # Must be the declared undefined-weight failure, never an epsilon fix.
        assert exc.value.code == "estimation_error"
