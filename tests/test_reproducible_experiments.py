"""Reproducible-experiment tests over the seeded synthetic DGP.

These are the three experiments the brief calls out -- known endogeneity,
weak instruments, collinear instruments -- with concrete expected behavior.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.dgp import DGPSpec, generate, standard_names
from app.kernel import estimate as kernel_estimate


def _estimate_direct(spec: DGPSpec, cov_type: str = "conventional"):
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
    return ds, res


@pytest.mark.experiment
def test_seeded_generation_is_bit_reproducible():
    spec = DGPSpec(n=1000, n_instruments=2, seed=777)
    a = generate(spec).columns["y"]
    b = generate(spec).columns["y"]
    assert a == b


@pytest.mark.experiment
def test_experiment_known_endogeneity_iv_beats_ols():
    spec = DGPSpec(n=10000, n_instruments=2, endogeneity_rho=0.9,
                   instrument_strength=0.9, beta=(2.0,), seed=801)
    ds, res = _estimate_direct(spec)
    iv_beta = res.beta[-1]

    y = np.asarray(ds.columns["y"])
    x1 = np.asarray(ds.columns["x1"])
    w1 = np.asarray(ds.columns["w1"])
    R = np.column_stack([np.ones(len(y)), w1, x1])
    b_ols = np.linalg.solve(R.T @ R, R.T @ y)[-1]

    assert abs(iv_beta - 2.0) < 0.05
    assert abs(b_ols - 2.0) > 0.3  # OLS severely biased
    assert res.endogeneity.p_value < 1e-50


@pytest.mark.experiment
def test_experiment_zero_endogeneity_ols_and_iv_agree():
    # Sanity check of the other direction: with rho=0 the two should be close.
    spec = DGPSpec(n=12000, n_instruments=2, endogeneity_rho=0.0,
                   instrument_strength=1.0, beta=(1.0,), seed=802)
    ds, res = _estimate_direct(spec)
    assert res.endogeneity.p_value > 0.05
    y = np.asarray(ds.columns["y"])
    x1 = np.asarray(ds.columns["x1"])
    w1 = np.asarray(ds.columns["w1"])
    R = np.column_stack([np.ones(len(y)), w1, x1])
    b_ols = np.linalg.solve(R.T @ R, R.T @ y)[-1]
    iv_beta = res.beta[-1]
    assert abs(b_ols - iv_beta) < 0.03


@pytest.mark.experiment
def test_experiment_weak_instrument_bias_direction_and_diagnosis():
    spec = DGPSpec(n=5000, n_instruments=2, endogeneity_rho=0.8,
                   instrument_strength=0.02, beta=(1.0,), seed=803)
    ds, res = _estimate_direct(spec)
    # Weak-IV 2SLS biases toward OLS; the point estimate is markedly off truth.
    assert res.cragg_donald < 10
    assert abs(res.beta[-1] - 1.0) > 0.1


@pytest.mark.experiment
def test_experiment_strength_monotonicity_of_cragg_donald():
    # Stronger first stage -> larger Cragg-Donald statistic (same seed/noise).
    f_weak = _estimate_direct(
        DGPSpec(n=4000, n_instruments=2, instrument_strength=0.05, seed=804)
    )[1].cragg_donald
    f_strong = _estimate_direct(
        DGPSpec(n=4000, n_instruments=2, instrument_strength=1.0, seed=804)
    )[1].cragg_donald
    assert f_strong > 50 * f_weak


@pytest.mark.experiment
def test_experiment_collinear_instruments_vif_rises_and_rank_info_present():
    spec = DGPSpec(n=4000, n_instruments=3, collinear_instruments=True,
                   instrument_strength=0.9, seed=805)
    _, res = _estimate_direct(spec)
    vifs = list(res.rank.vif.values())
    assert max(vifs) > 1000
    assert len(res.rank.first_stage_svals) == 1  # K=1 -> one singular value
