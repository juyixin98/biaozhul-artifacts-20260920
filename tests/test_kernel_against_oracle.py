"""Kernel statistics vs the independently derived matrix oracle.

These tests assert *concrete numerical values*: point estimates equal the
closed-form IV formula, standard errors equal the CORRECT 2SLS formula and
provably differ from the naive second-stage OLS formula.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.dgp import DGPSpec
from app.kernel import estimate as kernel_estimate

from .oracle import ols, oracle_iv


def _kernel(spec: DGPSpec, cov_type: str = "conventional"):
    from app.dgp import generate, standard_names
    ds = generate(spec)
    nm = standard_names(spec)
    y = np.asarray(ds.columns["y"])
    X = np.column_stack([ds.columns[n] for n in nm["endogenous"]])
    W = np.column_stack([ds.columns[n] for n in nm["exogenous"]])
    Z = np.column_stack([ds.columns[n] for n in nm["instruments"]])
    res = kernel_estimate(
        y, X, np.column_stack([np.ones(len(y)), W]), Z,
        names_endog=tuple(nm["endogenous"]),
        names_exog=tuple(nm["exogenous"]),
        names_instruments=tuple(nm["instruments"]),
        add_constant=True, cov_type=cov_type, alpha=0.05,
        rank_rcond=1e-9, run_overid=True, run_endogeneity=True,
    )
    return ds, y, np.column_stack([np.ones(len(y)), W]), X, Z, res


@pytest.mark.unit
def test_point_estimates_match_closed_form_iv():
    spec = DGPSpec(n=5000, n_instruments=2, seed=101)
    ds, y, Q, X, Z, res = _kernel(spec)
    ref = oracle_iv(y, X, Q[:, 1:], Z, add_constant=True)
    np.testing.assert_allclose(res.beta, ref.beta, rtol=1e-9, atol=1e-10)


@pytest.mark.unit
def test_conventional_se_is_correct_2sls_formula_not_naive_second_stage():
    spec = DGPSpec(n=5000, n_instruments=2, endogeneity_rho=0.8, seed=102)
    ds, y, Q, X, Z, res = _kernel(spec)
    ref = oracle_iv(y, X, Q[:, 1:], Z, add_constant=True)

    se_correct = np.sqrt(np.diag(ref.vcv_conventional))
    se_naive = np.sqrt(np.diag(ref.vcv_naive_second_stage))
    se_kernel = np.array([c.std_error for c in res.coefficients])

    # The service MUST match the correct 2SLS VCV ...
    np.testing.assert_allclose(se_kernel, se_correct, rtol=1e-9, atol=1e-10)
    # ... and must provably NOT be the naive second-stage OLS formula.
    # With endogenous regressors the two residual variances differ.
    assert np.max(np.abs(se_correct - se_naive)) > 1e-5, (
        "correct and naive SEs coincide; test setup would not distinguish them"
    )
    np.testing.assert_allclose(
        res.residuals, ref.residuals, rtol=1e-9, atol=1e-10
    )
    # Structural residuals must use original X, not fitted X.
    assert not np.allclose(ref.residuals, ref.second_stage_residuals_wrong)


@pytest.mark.unit
def test_robust_sandwich_se_matches_oracle():
    spec = DGPSpec(n=5000, n_instruments=2, seed=103)
    ds, y, Q, X, Z, res = _kernel(spec, cov_type="robust")
    ref = oracle_iv(y, X, Q[:, 1:], Z, add_constant=True)
    se_robust = np.sqrt(np.diag(ref.vcv_robust))
    se_kernel = np.array([c.std_error for c in res.coefficients])
    np.testing.assert_allclose(se_kernel, se_robust, rtol=1e-9, atol=1e-10)


@pytest.mark.unit
def test_cragg_donald_equals_partial_f_with_one_endogenous_regressor():
    spec = DGPSpec(n=4000, n_instruments=3, seed=104)
    ds, y, Q, X, Z, res = _kernel(spec)
    ref = oracle_iv(y, X, Q[:, 1:], Z, add_constant=True)
    assert res.cragg_donald == pytest.approx(ref.cragg_donald, rel=1e-9)
    assert res.cragg_donald == pytest.approx(ref.partial_f[0], rel=1e-9)
    assert res.first_stage[0].f_stat == pytest.approx(res.cragg_donald, rel=1e-10)


@pytest.mark.unit
def test_2sls_recovers_truth_and_ols_is_biased_under_known_endogeneity():
    spec = DGPSpec(n=20000, n_instruments=2, endogeneity_rho=0.8,
                   instrument_strength=0.8, beta=(1.0,), seed=105)
    ds, y, Q, X, Z, res = _kernel(spec)
    true_beta = 1.0

    b_ols, vcv_ols = ols(y, np.hstack([Q, X]))
    ols_beta = b_ols[-1]
    ols_se = np.sqrt(np.diag(vcv_ols))[-1]

    iv_idx = list(res.coefficient_names).index("x1")
    iv_beta = res.beta[iv_idx]
    iv_se = res.coefficients[iv_idx].std_error

    # OLS is badly biased: truth far outside a narrow OLS band.
    assert abs(ols_beta - true_beta) > 10 * ols_se
    # 2SLS recovers the truth within ~2 robust SE.
    assert abs(iv_beta - true_beta) < 3 * iv_se
    # DWH strongly rejects exogeneity.
    assert res.endogeneity.p_value < 1e-10


@pytest.mark.unit
def test_sargan_statistic_matches_oracle_and_does_not_reject_valid_instruments():
    spec = DGPSpec(n=8000, n_instruments=3, seed=106)
    ds, y, Q, X, Z, res = _kernel(spec)
    ref = oracle_iv(y, X, Q[:, 1:], Z, add_constant=True)
    assert res.overid is not None and res.overid.kind == "sargan"
    assert res.overid.statistic == pytest.approx(ref.sargan, rel=1e-9)
    assert res.overid.p_value > 0.10  # valid instruments -> not rejected


@pytest.mark.unit
def test_two_step_gmm_matches_oracle_and_is_close_to_2sls_when_strong():
    spec = DGPSpec(n=8000, n_instruments=3, instrument_strength=0.8, seed=107)
    ds, y, Q, X, Z, res = _kernel(spec, cov_type="robust")
    ref = oracle_iv(y, X, Q[:, 1:], Z, add_constant=True)
    np.testing.assert_allclose(res.gmm_beta, ref.gmm_beta, rtol=1e-8, atol=1e-9)
    assert res.overid is not None and res.overid.kind == "hansen_j"
    assert res.overid.statistic == pytest.approx(ref.hansen_j, rel=1e-8)
    np.testing.assert_allclose(res.gmm_beta, res.beta, atol=0.03)
    assert res.overid.p_value > 0.10


@pytest.mark.unit
def test_dwh_statistic_matches_oracle():
    spec = DGPSpec(n=4000, n_instruments=2, endogeneity_rho=0.6, seed=108)
    ds, y, Q, X, Z, res = _kernel(spec)
    ref = oracle_iv(y, X, Q[:, 1:], Z, add_constant=True)
    assert res.endogeneity.statistic == pytest.approx(ref.dwh_stat, rel=1e-8)


@pytest.mark.unit
def test_multi_endogenous_estimator_matches_oracle():
    spec = DGPSpec(n=12000, n_endogenous=2, n_instruments=4,
                   instrument_strength=0.9, beta=(1.0, -2.0), seed=109)
    ds, y, Q, X, Z, res = _kernel(spec)
    ref = oracle_iv(y, X, Q[:, 1:], Z, add_constant=True)
    np.testing.assert_allclose(res.beta, ref.beta, rtol=1e-8, atol=1e-9)
    assert res.cragg_donald == pytest.approx(ref.cragg_donald, rel=1e-8)
    np.testing.assert_allclose(
        [f.f_stat for f in res.first_stage], ref.partial_f, rtol=1e-8
    )
    # Both structural coefficients recovered.
    assert abs(res.beta[-2] - 1.0) < 0.1
    assert abs(res.beta[-1] - (-2.0)) < 0.1
