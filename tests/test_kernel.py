"""Unit tests for the 2SLS kernel math.

Every point estimate and standard error is checked against an oracle that
shares no code path with the kernel (explicit projection matrices + LAPACK
inverse, numerical GMM optimization, and indirect least squares).
"""
from __future__ import annotations

import numpy as np
import numpy.linalg as la
import pytest

from twosls.data import build_data
from twosls.kernel import run_kernel
from conftest import make_request
from reference import (
    bootstrap_reference_se,
    iv_2sls_dense,
    iv_gmm_optimize,
    iv_ils,
)

pytestmark = pytest.mark.unit


def _artifacts(sample, covariance="homoskedastic"):
    req = make_request(sample, request_id="kernel", covariance=covariance)
    data = build_data(req)
    return req, data, run_kernel(data, covariance=covariance, request_id="kernel")


def test_point_estimate_matches_dense_projection_oracle(strong_sample):
    req, _, art = _artifacts(strong_sample)
    ref = iv_2sls_dense(req.columns, req.spec)
    np.testing.assert_allclose(art.delta, ref.delta, rtol=1e-10, atol=1e-10)


def test_point_estimate_matches_numerical_gmm_oracle(strong_sample):
    req, _, art = _artifacts(strong_sample)
    gmm_delta = iv_gmm_optimize(req.columns, req.spec)
    np.testing.assert_allclose(art.delta, gmm_delta, rtol=1e-7, atol=1e-7)


def test_just_identified_matches_indirect_least_squares(just_identified_sample_fixture):
    req = make_request(just_identified_sample_fixture, request_id="kernel-ils")
    data = build_data(req)
    art = run_kernel(data, request_id="kernel-ils")
    ils_delta = iv_ils(req.columns, req.spec)
    np.testing.assert_allclose(art.delta, ils_delta, rtol=1e-10, atol=1e-10)


def test_homoskedastic_standard_errors_match_matrix_formula(strong_sample):
    req, _, art = _artifacts(strong_sample)
    ref = iv_2sls_dense(req.columns, req.spec)
    se = np.sqrt(np.diag(art.vcov))
    np.testing.assert_allclose(se, ref.se, rtol=1e-10, atol=1e-12)


def test_robust_standard_errors_match_meat_bread_formula(strong_sample):
    req, _, art = _artifacts(strong_sample, covariance="robust")
    ref = iv_2sls_dense(req.columns, req.spec)
    se = np.sqrt(np.diag(art.vcov))
    np.testing.assert_allclose(se, ref.se_robust, rtol=1e-10, atol=1e-12)


def test_standard_errors_are_NOT_naive_second_stage_ols(strong_sample):
    """The central correctness requirement.

    Feeding fitted first-stage values into a plain OLS routine yields a
    residual variance based on y - W2 delta, which uses the WRONG residual
    (Y != Y_hat). The kernel's SE must differ from that naive computation
    and agree instead with the structural-residual formula.
    """
    req, data, art = _artifacts(strong_sample)
    n, r = data.nobs, data.n_endog + data.n_exog
    W2 = np.column_stack([art.Y_hat, data.X])

    naive_resid = data.y - W2 @ art.delta
    naive_sigma2 = naive_resid @ naive_resid / (n - r)
    naive_vcov = naive_sigma2 * la.inv(W2.T @ W2)
    naive_se = np.sqrt(np.diag(naive_vcov))

    correct_se = np.sqrt(np.diag(art.vcov))
    ref = iv_2sls_dense(req.columns, req.spec)

    # the two formulas are genuinely different numbers here
    assert not np.allclose(naive_se, correct_se, rtol=1e-4)
    # the kernel is on the correct-formula side
    np.testing.assert_allclose(correct_se, ref.se, rtol=1e-10, atol=1e-12)
    # and the naive SE is inflated by the ignored first-stage residual
    assert np.mean(naive_se / correct_se) > 1.05


def test_first_stage_projection_uses_full_instrument_set(strong_sample):
    """Y_hat must equal P_[X,Z] Y, not OLS fitted values of Y on X alone."""
    req = make_request(strong_sample, request_id="fs")
    data = build_data(req)
    art = run_kernel(data, request_id="fs")
    Q = np.column_stack([data.X, data.Z])
    expected = Q @ la.solve(Q.T @ Q, Q.T @ data.Y)
    np.testing.assert_allclose(art.Y_hat, expected, rtol=1e-10, atol=1e-12)

    coef_x = la.lstsq(data.X, data.Y, rcond=None)[0]
    ols_x_fit = data.X @ coef_x
    # instruments materially change the fitted values
    assert np.abs(art.Y_hat - ols_x_fit).mean() > 1e-3


def test_structural_residuals_are_used_for_sigma2(strong_sample):
    _, data, art = _artifacts(strong_sample)
    r = data.n_endog + data.n_exog
    W = np.column_stack([data.Y, data.X])
    expected_sigma2 = (data.y - W @ art.delta) @ (data.y - W @ art.delta) / (data.nobs - r)
    assert art.sigma2 == pytest.approx(expected_sigma2, rel=1e-12)


def test_kernel_bootstrap_se_close_to_independent_bootstrap(strong_sample):
    req = make_request(strong_sample, request_id="boot", bootstrap_reps=400)
    from twosls.estimator import _bootstrap_se, build_data as bd

    data = bd(req)
    rng = np.random.default_rng(20260928)
    se = _bootstrap_se(data, 400, rng)
    ref_se = bootstrap_reference_se(req.columns, req.spec, reps=400)
    # Both bootstrap the same estimand with independent RNGs: allow MC slack.
    np.testing.assert_allclose(se, ref_se, rtol=0.25, atol=0.01)


def test_multi_endog_coefficients_match_dense_oracle():
    from experiments.dgp import multi_endog_sample
    from twosls.contract import EstimationOptions, InstrumentValidityClaim, ModelSpec

    sample = multi_endog_sample()
    cols = {k: v.tolist() for k, v in sample.columns.items()}
    spec = ModelSpec(
        dependent="y",
        endogenous=["y_end1", "y_end2"],
        included_exogenous=["const"],
        excluded_instruments=["z1", "z2", "z3"],
    )
    req = make_request(sample, request_id="multi", spec=spec)
    req.columns = cols
    data = build_data(req)
    art = run_kernel(data, request_id="multi")
    ref = iv_2sls_dense(cols, spec)
    np.testing.assert_allclose(art.delta, ref.delta, rtol=1e-9, atol=1e-9)
