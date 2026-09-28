"""Diagnostic decision tests across known synthetic scenarios.

These assert the *specific* verdict and failure category, not merely that the
function returns. Reference scenarios come from the known generative process
in :mod:`ipw_ate.synthetic`, independent of the estimator.
"""

import numpy as np
import pytest

from ipw_ate.contract import Decision, Estimand, IPWConfig, ObservationSet
from ipw_ate.diagnostics import build_diagnostic, count_single_arm_cells
from ipw_ate.errors import OverlapViolationError
from ipw_ate.pipeline import run_ipw
from ipw_ate.propensity import cross_fit_propensity
from ipw_ate.synthetic import (
    make_extreme_weights_data,
    make_no_overlap_data,
    make_overlap_data,
    make_separated_data,
)
from ipw_ate.weights import build_weights


def _diag_for(data, cfg, request_id="t"):
    ps, folds, membership = cross_fit_propensity(data.treatment, data.covariates, cfg)
    w = build_weights(data.treatment, ps, cfg)
    return build_diagnostic(
        ObservationSet(data.treatment, data.outcome, data.covariates,
                       data.feature_names),
        w, ps, folds, membership, cfg, request_id,
    ), ps, w


def test_good_overlap_is_accepted():
    cfg = IPWConfig(n_splits=5)
    diag, _, _ = _diag_for(make_overlap_data(800, seed=11), cfg)
    assert diag.decision is Decision.ACCEPT
    assert diag.reasons == ()
    assert diag.request_id == "t"
    assert diag.ess_treated >= cfg.min_ess_per_arm
    assert diag.ess_control >= cfg.min_ess_per_arm
    assert 0.0 < diag.score_min <= diag.score_max < 1.0


def test_no_overlap_region_is_rejected_with_cell_reason():
    cfg = IPWConfig(n_splits=4)
    data = make_no_overlap_data(800, seed=44)
    diag, _, _ = _diag_for(data, cfg)
    assert diag.decision is Decision.REJECT
    assert "no_overlap_cell" in diag.reasons
    assert diag.single_arm_cells >= 1


def test_count_single_arm_cells_direct_construction():
    # Independent direct construction: treated-only cluster at +4 and a
    # control-only cluster at -4, well clear of the background support.
    x = np.vstack([
        np.column_stack([np.full(30, 4.0), np.zeros(30)]),
        np.column_stack([np.full(30, -4.0), np.zeros(30)]),
        np.random.default_rng(0).normal(size=(120, 2)),
    ])
    t = np.concatenate([np.ones(30, int), np.zeros(30, int),
                        np.random.default_rng(1).integers(0, 2, 120)])
    obs = ObservationSet(t, np.zeros(x.shape[0]), x, ("x0", "x1"))
    # One void on the high side (treated-only) and one on the low side.
    assert count_single_arm_cells(obs) >= 2


def test_extreme_weights_are_inconclusive_or_reject_not_silent():
    cfg = IPWConfig(n_splits=5)
    data = make_extreme_weights_data(1000, seed=22)
    diag, _, _ = _diag_for(data, cfg)
    # Steep logit yields extreme interior scores: weak overlap is surfaced.
    assert diag.decision in {Decision.INCONCLUSIVE, Decision.REJECT}
    assert diag.reasons  # non-empty explanation
    assert diag.n_extreme_scores >= 1 or diag.max_weight > 10.0


def test_pipeline_good_data_recovers_known_ate():
    cfg = IPWConfig(n_splits=5, random_seed=20260927)
    data = make_overlap_data(2000, seed=101)
    result = run_ipw(data.treatment, data.outcome, data.covariates,
                     config=cfg, request_id="known-ate")
    assert result.diagnostic.decision is Decision.ACCEPT
    # True constant ATE is 2.0; cross-fitted IPW should be close.
    assert result.estimate == pytest.approx(2.0, abs=0.35)
    assert result.ci_lower < 2.0 < result.ci_upper
    assert result.request_id == "known-ate"


def test_pipeline_rejects_no_overlap_with_classified_error():
    cfg = IPWConfig(n_splits=4)
    data = make_no_overlap_data(800, seed=44)
    with pytest.raises(OverlapViolationError) as exc:
        run_ipw(data.treatment, data.outcome, data.covariates,
                config=cfg, request_id="no-ov")
    assert exc.value.code == "overlap_violation_error"
    assert exc.value.diagnostic.decision is Decision.REJECT
    assert "no_overlap_cell" in exc.value.diagnostic.reasons


def test_pipeline_separated_data_is_undefined():
    cfg = IPWConfig(n_splits=4)
    data = make_separated_data(400, seed=55)
    from ipw_ate.errors import ModelSeparationError, PropensityScoreError
    with pytest.raises((ModelSeparationError, PropensityScoreError)) as exc:
        run_ipw(data.treatment, data.outcome, data.covariates,
                config=cfg, request_id="sep")
    assert exc.value.code in {"model_separation_error",
                              "propensity_score_error"}


def test_diagnostic_carries_assumptions_and_redacted_fields():
    cfg = IPWConfig(n_splits=5)
    diag, _, _ = _diag_for(make_overlap_data(500, seed=11), cfg, "rid-xyz")
    joined = " ".join(diag.assumptions).lower()
    assert "unconfoundedness" in joined and "positivity" in joined
    # Packet holds aggregates only; no raw outcome/covariate array field.
    public = diag.__dataclass_fields__.keys()
    assert "outcome" not in public and "covariates" not in public


def test_heavy_tailed_common_support_is_not_falsely_rejected():
    # Log-normal covariates with a well-specified propensity share common
    # support; sparse tail points must not trigger a no-overlap REJECT.
    rng = np.random.default_rng(0)
    n = 1000
    x = np.column_stack([rng.lognormal(0, 0.8, n), rng.normal(size=n)])
    x = (x - x.mean(0)) / x.std(0)
    p = 1 / (1 + np.exp(-(0.7 * x[:, 0] - 0.5 * x[:, 1])))
    t = (rng.uniform(size=n) < p).astype(int)
    y = x[:, 0] - x[:, 1] + 2 * t + rng.normal(scale=0.5, size=n)
    cfg = IPWConfig(n_splits=5)
    result = run_ipw(t, y, x, config=cfg, request_id="heavytail")
    # May be inconclusive (extreme weights) but never a structural overlap reject.
    assert result.diagnostic.decision in (Decision.ACCEPT, Decision.INCONCLUSIVE)
    assert "no_overlap_cell" not in result.diagnostic.reasons
