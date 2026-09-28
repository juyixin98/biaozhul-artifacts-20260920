"""Weighted covariate-balance diagnostic tests.

The small-sample z is derived by hand below (not produced by the kernel);
scenario tests confirm the balance screen separates *propensity*
misspecification (must flag) from *outcome* misspecification (IPW is robust).
"""

import math

import numpy as np

from ipw_ate.balance import (
    augmented_basis,
    max_balance_z,
    weighted_balance_z,
)


def test_augmented_basis_columns():
    x = np.array([[1.0, 2.0], [3.0, 4.0]])
    b = augmented_basis(x)
    # [x0, x1, x0^2, x1^2, x0*x1]
    assert b.shape == (2, 5)
    np.testing.assert_allclose(b[0], [1, 2, 1, 4, 2])
    np.testing.assert_allclose(b[1], [3, 4, 9, 16, 12])


def test_balance_z_hand_calculation():
    # One basis column; unit weights.
    # treated values [0,2] -> mean 1, var (1^2+(-1)^2)/2^2 = 0.5
    # control values [-2,0] -> mean -1, var 0.5
    # se = sqrt(1.0) = 1, mean difference = 2 -> z = 2
    t = np.array([1, 1, 0, 0])
    w = np.ones(4)
    basis = np.array([[0.0], [2.0], [-2.0], [0.0]])
    z = weighted_balance_z(t, w, basis)
    assert math.isclose(z[0], 2.0, rel_tol=1e-12)


def test_balanced_design_has_small_z():
    # Same treated/control distribution -> z near 0.
    rng = np.random.default_rng(0)
    x = rng.normal(size=(2000, 1))
    t = (rng.uniform(size=2000) < 0.5).astype(int)
    z = weighted_balance_z(t, np.ones(2000), x)
    assert z[0] < 2.0


def test_propensity_misspecification_flagged_end_to_end():
    from ipw_ate.contract import IPWConfig
    from ipw_ate.errors import OverlapViolationError
    from ipw_ate.pipeline import run_ipw
    from ipw_ate.synthetic import make_propensity_misspecified_data
    import pytest

    data = make_propensity_misspecified_data(2000, seed=71)
    with pytest.raises(OverlapViolationError) as exc:
        run_ipw(data.treatment, data.outcome, data.covariates,
                config=IPWConfig(n_splits=5), request_id="bal-reject")
    d = exc.value.diagnostic
    assert "covariate_imbalance" in d.reasons
    assert d.max_balance_z > 4.0
    assert "squares" in d.balance_basis or "interactions" in d.balance_basis


def test_outcome_misspecification_is_not_flagged():
    from ipw_ate.contract import Decision, IPWConfig
    from ipw_ate.pipeline import run_ipw
    from ipw_ate.synthetic import make_misspecified_data

    data = make_misspecified_data(2000, seed=33)
    # Correct propensity, non-linear outcome: IPW stays valid and the balance
    # screen must accept (no covariate_imbalance reason).
    result = run_ipw(data.treatment, data.outcome, data.covariates,
                     config=IPWConfig(n_splits=5), request_id="bal-ok")
    assert result.diagnostic.decision in (Decision.ACCEPT, Decision.INCONCLUSIVE)
    assert "covariate_imbalance" not in result.diagnostic.reasons
    assert result.diagnostic.max_balance_z < 3.0
    assert abs(result.estimate - 2.0) < 0.5
