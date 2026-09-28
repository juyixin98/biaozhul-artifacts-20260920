"""Optional cross-check against the mature ``linearmodels`` package.

Skipped automatically when linearmodels is not installed
(``pip install -r requirements-optional.txt``). This is an *external*
implementation maintained independently of this repository.
"""
from __future__ import annotations

import numpy as np
import pytest

from twosls.estimator import estimate
from conftest import make_request

pytestmark = pytest.mark.oracle

lm = pytest.importorskip("linearmodels")


def _columns(sample):
    y = np.asarray(sample.columns["y"])
    Y = np.asarray(sample.columns["x_end"])[:, None]
    X = np.column_stack(
        [np.ones(len(y))]
        + [np.asarray(sample.columns[f"w{j+1}"]) for j in range(sample.config.n_controls)]
    )
    Z = np.column_stack([np.asarray(sample.columns[f"z{j+1}"])
                         for j in range(sample.config.n_instruments)])
    return y, X, Y, Z


@pytest.mark.parametrize("cov_kind,lm_cov", [("homoskedastic", "unadjusted"), ("robust", "robust")])
def test_matches_linearmodels_iv2sls(strong_sample, cov_kind, lm_cov):
    from linearmodels.iv import IV2SLS

    y, X, Y, Z = _columns(strong_sample)
    ref = IV2SLS(y, X, Y, Z).fit(cov_type=lm_cov, debiased=True)

    out = estimate(make_request(strong_sample, request_id=f"lm-{cov_kind}", covariance=cov_kind))
    # With numpy inputs linearmodels names columns positionally:
    # X=[const, w1] -> 'exog.0','exog.1'; Y -> 'endog'.
    our = {c.name: c for c in out.coefficients}
    our_delta = np.array([our["x_end"].estimate, our["w1"].estimate, our["const"].estimate])
    lm_delta = np.array([ref.params["endog"], ref.params["exog.1"], ref.params["exog.0"]])
    np.testing.assert_allclose(our_delta, lm_delta, rtol=1e-8, atol=1e-8)

    our_se = np.array([our["x_end"].std_error, our["w1"].std_error, our["const"].std_error])
    lm_se = np.array(
        [ref.std_errors["endog"], ref.std_errors["exog.1"], ref.std_errors["exog.0"]]
    )
    np.testing.assert_allclose(our_se, lm_se, rtol=1e-6, atol=1e-8)


def test_matches_linearmodels_first_stage_f(weak_sample):
    from linearmodels.iv import IV2SLS

    y, X, Y, Z = _columns(weak_sample)
    ref = IV2SLS(y, X, Y, Z).fit(cov_type="unadjusted", debiased=True)
    fs = ref.first_stage
    lm_f = float(fs.diagnostics.loc["endog", "f.stat"])

    out = estimate(make_request(weak_sample, request_id="lm-fs"))
    our_f = out.first_stage[0].f_statistic
    # linearmodels uses the n-denominator Wald convention (F * n/(n-q));
    # the classical F(L, n-m-L) we report differs by exactly that ~0.2%.
    assert our_f == pytest.approx(lm_f, rel=5e-3)
