"""Target-population (ATE vs ATT) contract tests."""

import numpy as np

from ipw_ate.contract import Estimand, IPWConfig
from ipw_ate.pipeline import run_ipw
from ipw_ate.synthetic import make_overlap_data


def test_att_treated_arm_has_unit_weight_and_recovers_effect():
    cfg = IPWConfig(estimand=Estimand.ATT, n_splits=5)
    data = make_overlap_data(3000, seed=7)
    result = run_ipw(data.treatment, data.outcome, data.covariates,
                     config=cfg, request_id="att-check")
    # Treated define the target population -> their weights are exactly 1.
    np.testing.assert_allclose(result.weights[data.treatment == 1], 1.0)
    # Constant additive effect: true ATT = 2.0 as well.
    assert abs(result.estimate - 2.0) < 0.4
    assert result.estimand == "ATT"


def test_ate_and_att_estimands_are_distinct_configs():
    data = make_overlap_data(2000, seed=9)
    ate = run_ipw(data.treatment, data.outcome, data.covariates,
                  config=IPWConfig(estimand=Estimand.ATE), request_id="a")
    att = run_ipw(data.treatment, data.outcome, data.covariates,
                  config=IPWConfig(estimand=Estimand.ATT), request_id="b")
    assert ate.estimand == "ATE" and att.estimand == "ATT"
    # Control weights differ by construction (1/(1-e) vs e/(1-e) raw).
    ctrl = data.treatment == 0
    assert not np.allclose(ate.weights[ctrl], att.weights[ctrl])
