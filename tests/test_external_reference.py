"""Optional cross-check against the mature ``linearmodels`` package.

The independent matrix oracle in ``tests/oracle.py`` always runs. This module
additionally compares against a widely used third-party implementation when it
is installed in the environment (``pip install linearmodels``); the whole file
skips itself when the package is absent, so the suite stays self-contained.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.dgp import DGPSpec, generate, standard_names
from app.kernel import estimate as kernel_estimate

linearmodels = pytest.importorskip(
    "linearmodels", reason="linearmodels not installed; oracle cross-checks still run"
)
pandas = pytest.importorskip("pandas")
from linearmodels.iv import IV2SLS  # noqa: E402


def _fit_both(spec: DGPSpec, cov_type: str):
    ds = generate(spec)
    nm = standard_names(spec)
    y = np.asarray(ds.columns["y"])
    X = np.column_stack([ds.columns[n] for n in nm["endogenous"]])
    W = np.column_stack([ds.columns[n] for n in nm["exogenous"]])
    Z = np.column_stack([ds.columns[n] for n in nm["instruments"]])
    ones = np.ones((len(y), 1))

    res = kernel_estimate(
        y, X, ones if W.shape[1] == 0 else np.column_stack([ones, W]), Z,
        names_endog=tuple(nm["endogenous"]),
        names_exog=tuple(nm["exogenous"]),
        names_instruments=tuple(nm["instruments"]),
        add_constant=True, cov_type=cov_type, alpha=0.05,
        rank_rcond=1e-9, run_overid=True, run_endogeneity=True,
    )

    dep = pandas.Series(y, name="y")
    exog = pandas.DataFrame(
        ones if W.shape[1] == 0 else np.column_stack([ones, W]),
        columns=["const"] + nm["exogenous"],
    )
    endog = pandas.DataFrame(X, columns=nm["endogenous"])
    instr = pandas.DataFrame(Z, columns=nm["instruments"])
    lm_cov = "unadjusted" if cov_type == "conventional" else "robust"
    lm = IV2SLS(dep, exog, endog, instr).fit(cov_type=lm_cov)
    return res, lm


@pytest.mark.reference
def test_matches_linearmodels_point_estimates_and_se_unadjusted():
    spec = DGPSpec(n=6000, n_instruments=3, endogeneity_rho=0.7, seed=901)
    res, lm = _fit_both(spec, "conventional")
    np.testing.assert_allclose(
        res.beta, lm.params[list(res.coefficient_names)].to_numpy(),
        rtol=1e-8, atol=1e-9,
    )
    np.testing.assert_allclose(
        [c.std_error for c in res.coefficients],
        lm.std_errors[list(res.coefficient_names)].to_numpy(),
        rtol=1e-8, atol=1e-9,
    )


@pytest.mark.reference
def test_matches_linearmodels_robust_se():
    spec = DGPSpec(n=6000, n_instruments=3, endogeneity_rho=0.7, seed=902)
    res, lm = _fit_both(spec, "robust")
    np.testing.assert_allclose(
        [c.std_error for c in res.coefficients],
        lm.std_errors[list(res.coefficient_names)].to_numpy(),
        rtol=2e-3, atol=1e-6,
    )


@pytest.mark.reference
def test_matches_linearmodels_sargan():
    spec = DGPSpec(n=6000, n_instruments=3, seed=903)
    res, lm = _fit_both(spec, "conventional")
    sargan = getattr(lm, "sargan", None)
    if sargan is None:
        pytest.skip("installed linearmodels exposes no sargan attribute")
    assert res.overid is not None
    assert res.overid.statistic == pytest.approx(float(sargan.stat), rel=1e-6)
    assert res.overid.p_value == pytest.approx(float(sargan.pval), rel=1e-6)


@pytest.mark.reference
def test_matches_linearmodels_cragg_donald():
    spec = DGPSpec(n=6000, n_instruments=3, seed=904)
    res, lm = _fit_both(spec, "conventional")
    cd = getattr(lm, "cragg_donald", None)
    if cd is None:
        pytest.skip("installed linearmodels exposes no cragg_donald attribute")
    assert res.cragg_donald == pytest.approx(float(cd.stat), rel=1e-6)
