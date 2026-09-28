"""Kernel tests against HAND-COMPUTED numbers (tests/fixtures/tiny_weights.json).

These tests pin the exact weight formulas, ESS and the no-denominator-
replacement rule. The expected values come from paper arithmetic documented
in the fixture and README, never from the estimator under test.
"""

from __future__ import annotations

import numpy as np
import pytest

from ipwate.config import ClippingConfig
from ipwate.errors import PositivityError
from ipwate.weights import compute_weighted_estimation, effective_sample_size

_NO_CLIP = ClippingConfig(enabled=False)


def _run(a, y, p, *, estimand="ate", weight_type="stabilized", clipping=_NO_CLIP):
    return compute_weighted_estimation(
        a,
        y,
        p,
        estimand=estimand,
        weight_type=weight_type,
        clipping=clipping,
        positivity_eps=1e-6,
    )


def test_ate_stabilized_matches_hand_arithmetic(tiny_arrays, tiny_fixture):
    a, y, p = tiny_arrays
    wr = _run(a, y, p)
    exp = tiny_fixture["expected"]["ate_stabilized"]
    np.testing.assert_allclose(wr.weights, exp["weights"], rtol=0, atol=1e-12)
    assert wr.mu1 == pytest.approx(exp["mu1"], abs=1e-12)
    assert wr.mu0 == pytest.approx(exp["mu0"], abs=1e-12)
    assert wr.point == pytest.approx(exp["point"], abs=1e-12)
    assert wr.marginal_treated == pytest.approx(0.5, abs=1e-12)


def test_ess_matches_hand_arithmetic(tiny_arrays, tiny_fixture):
    a, y, p = tiny_arrays
    wr = _run(a, y, p)
    exp = tiny_fixture["expected"]["ate_stabilized"]
    assert effective_sample_size(wr.weights[a == 1]) == pytest.approx(
        exp["ess_treated"], abs=1e-12
    )
    assert effective_sample_size(wr.weights[a == 0]) == pytest.approx(
        exp["ess_untreated"], abs=1e-12
    )


def test_ate_ht_weights_are_unstabilized(tiny_arrays, tiny_fixture):
    a, y, p = tiny_arrays
    wr = _run(a, y, p, weight_type="ht")
    exp = tiny_fixture["expected"]["ate_ht"]
    np.testing.assert_allclose(wr.weights, exp["weights"], rtol=0, atol=1e-12)
    # Hajek normalization: HT and stabilized weights give the same point.
    assert wr.point == pytest.approx(exp["point"], abs=1e-12)


def test_att_stabilized_treated_get_weight_one(tiny_arrays, tiny_fixture):
    a, y, p = tiny_arrays
    wr = _run(a, y, p, estimand="att")
    exp = tiny_fixture["expected"]["att_stabilized"]
    np.testing.assert_allclose(wr.weights, exp["weights"], rtol=0, atol=1e-12)
    np.testing.assert_allclose(wr.weights[a == 1], 1.0)
    assert wr.point == pytest.approx(exp["point"], abs=1e-12)
    assert wr.mu1 == pytest.approx(exp["mu1"], abs=1e-12)
    assert wr.mu0 == pytest.approx(exp["mu0"], abs=1e-12)


def test_atu_stabilized_untreated_get_weight_one(tiny_arrays, tiny_fixture):
    a, y, p = tiny_arrays
    wr = _run(a, y, p, estimand="atu")
    exp = tiny_fixture["expected"]["atu_stabilized"]
    np.testing.assert_allclose(wr.weights, exp["weights"], rtol=0, atol=1e-12)
    np.testing.assert_allclose(wr.weights[a == 0], 1.0)
    assert wr.point == pytest.approx(exp["point"], abs=1e-12)


def test_zero_pscore_on_treated_rejects_with_positivity_category(tiny_fixture):
    case = tiny_fixture["expected"]["positivity_case"]
    a = np.array(case["a"], dtype=np.int8)
    p = np.array(case["p"], dtype=np.float64)
    y = np.array(case["y"], dtype=np.float64)
    with pytest.raises(PositivityError) as exc_info:
        _run(a, y, p)
    err = exc_info.value
    assert err.code == case["expected_error_code"]
    assert err.details[case["expected_detail_key"]] == 1
    # The message must state that replacement is forbidden, not that it happened.
    assert "replacement" in err.message


def test_zero_pscore_on_untreated_is_relevant_for_ate_and_att():
    a = np.array([1, 1, 0, 0], dtype=np.int8)
    y = np.array([3.0, 2.0, 1.0, 0.0])
    p = np.array([0.5, 0.5, 0.5, 1.0])  # unit 4 untreated, 1-p = 0
    for estimand in ("ate", "att"):
        with pytest.raises(PositivityError) as exc_info:
            _run(a, y, p, estimand=estimand)
        assert exc_info.value.code == "positivity_violation"
        assert exc_info.value.details["untreated_one_minus_p_le_eps"] == 1
    # ATU gives untreated units weight 1: p=1 there is irrelevant -> no raise.
    wr = _run(a, y, p, estimand="atu")
    assert np.isfinite(wr.point)


def test_fixed_clipping_profile_is_recorded_and_applied(tiny_fixture):
    case = tiny_fixture["expected"]["clipping_case"]
    a = np.array(case["a"], dtype=np.int8)
    y = np.array(case["y"], dtype=np.float64)
    p = np.array(case["raw_pscores"], dtype=np.float64)
    profile = case["profile"]
    clip = ClippingConfig(
        enabled=True, lower=profile["lower"], upper=profile["upper"],
        profile="test_fixed",
    )
    wr = _run(a, y, p, clipping=clip)
    assert wr.n_clipped == case["n_clipped"]
    # Unit 1's USED score is exactly the fixed lower bound, not a rescued value.
    assert wr.p_used[0] == pytest.approx(profile["lower"], abs=1e-12)
    assert wr.p_raw[0] == pytest.approx(0.05, abs=1e-12)
    # Stabilized ATE weight for unit 1: 0.5 / 0.1 = 5.
    assert wr.weights[0] == pytest.approx(5.0, abs=1e-12)


def test_weights_are_nonnegative_and_target_arm_sums_are_positive(tiny_arrays):
    a, y, p = tiny_arrays
    for estimand in ("ate", "att", "atu"):
        wr = _run(a, y, p, estimand=estimand)
        assert np.all(wr.weights >= 0.0)
        assert wr.weights[a == 1].sum() > 0
        assert wr.weights[a == 0].sum() > 0
