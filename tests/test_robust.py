"""Tests for robust kernel costs and IRLS weights."""

import pytest

from pose_graph.robust import robust_cost, robust_weight


def test_linear_kernel_is_plain_ls():
    for s in [0.0, 0.5, 4.0, 100.0]:
        assert robust_cost("linear", s) == s
        assert robust_weight("linear", s) == 1.0


def test_robust_costs_are_like_linear_at_origin():
    # First-order behavior near zero: rho(s) ~ s for smooth kernels.
    for kernel, k in [("huber", 1.0), ("cauchy", 1.0), ("geman_mcclure", 1.0)]:
        s = 1e-8
        assert robust_cost(kernel, s, k) == pytest.approx(s, rel=1e-4)
        assert robust_weight(kernel, s, k) == pytest.approx(1.0, abs=1e-4)


def test_huber_piecewise_weight():
    k = 0.5
    assert robust_weight("huber", 0.1, k) == 1.0
    assert robust_weight("huber", k * k, k) == 1.0
    assert robust_weight("huber", 4.0, k) == pytest.approx(k / 2.0)


def test_outlier_weights_are_small_and_decreasing():
    s = 50.0
    w_huber = robust_weight("huber", s, 1.0)
    w_cauchy = robust_weight("cauchy", s, 1.0)
    w_gm = robust_weight("geman_mcclure", s, 1.0)
    assert 0.0 < w_gm < w_cauchy < w_huber < 1.0


def test_robust_cost_bounded_for_redescending_kernels():
    # Cauchy grows only logarithmically; Geman-McClure is bounded by k^2.
    assert robust_cost("geman_mcclure", 1e6, 1.0) < 1.0
    assert robust_cost("cauchy", 1e6, 1.0) < 20.0


def test_unknown_kernel_and_bad_parameter():
    with pytest.raises(ValueError):
        robust_cost("tukey", 1.0)
    with pytest.raises(ValueError):
        robust_weight("huber", 1.0, parameter=None)
    with pytest.raises(ValueError):
        robust_cost("cauchy", 1.0, parameter=-2.0)
