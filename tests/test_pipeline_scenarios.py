"""End-to-end pipeline tests against scenarios with a KNOWN DGP.

Ground truth comes from the synthetic generator's independently declared
treatment-effect function, never from the estimator. We assert:

* good-overlap data: point estimate near the true ATE and CI plausibly covers;
* no-overlap data: positivity violation with the exact failure category;
* poor-overlap data: diagnostics warn/reject via ESS/extreme weights;
* misspecified DGP: calibration findings flag the wrong model;
* clipping changes the recorded estimand and is disclosed.
"""

from __future__ import annotations

import numpy as np
import pytest

from ipwate.errors import PositivityError
from ipwate.pipeline import run_ipw
from ipwate.synthetic import generate_synthetic


def test_good_overlap_recovers_known_ate_and_accepts():
    data = generate_synthetic(n=4000, scenario="good_overlap", seed=2024)
    result = run_ipw(data.x, data.a, data.y, request_id="good-1")
    assert result.verdict in ("accept", "warn")
    # True ATE on the drawn sample is known exactly from tau(X).
    assert result.estimate.point == pytest.approx(data.ate_true_sample, abs=0.35)
    assert result.estimate.se > 0
    assert result.estimate.ci_lower < result.estimate.point < result.estimate.ci_upper
    # Contract is fully recorded.
    assert result.contract.estimand == "ate"
    assert result.contract.weight_type == "stabilized"
    assert result.contract.crossfit_n_splits == 5
    assert result.contract.clipping_enabled is False
    assert "not causal proof" in result.causal_disclaimer
    # Every fold used OOF predictions with both classes.
    assert len(result.folds) == 5
    assert all(f.converged for f in result.folds)
    assert all(f.n_train_treated > 0 and f.n_train_untreated > 0 for f in result.folds)


def test_ci_coverage_over_many_seeds_is_nominal():
    """Independent Monte Carlo coverage check using the known DGP."""
    covers = 0
    reps = 40
    for seed in range(1000, 1000 + reps):
        data = generate_synthetic(n=3000, scenario="good_overlap", seed=seed)
        r = run_ipw(data.x, data.a, data.y)
        if r.estimate.ci_lower <= data.ate_true_sample <= r.estimate.ci_upper:
            covers += 1
    rate = covers / reps
    # 95% nominal; allow sampling slack (very conservative lower bound 0.80).
    assert rate >= 0.80, f"coverage {rate} far below nominal 0.95"


def test_no_overlap_raises_positivity_with_category():
    data = generate_synthetic(n=2000, scenario="no_overlap", seed=9)
    with pytest.raises(PositivityError) as exc:
        run_ipw(data.x, data.a, data.y)
    assert exc.value.code == "positivity_violation"
    assert exc.value.details["estimand"] == "ate"
    assert (
        exc.value.details["treated_p_le_eps"]
        + exc.value.details["untreated_one_minus_p_le_eps"]
    ) > 0


def test_poor_overlap_without_clipping_is_rejected_or_warned_with_evidence():
    data = generate_synthetic(n=3000, scenario="poor_overlap", seed=11)
    # Either positivity rejects outright, or it survives and diagnostics fire.
    try:
        result = run_ipw(data.x, data.a, data.y)
    except PositivityError as exc:
        assert exc.code == "positivity_violation"
        return
    codes = {f.code for f in result.findings}
    assert result.verdict in ("warn", "reject")
    assert any(
        c.startswith(("ess_", "max_weight", "overlap_", "pscore_")) for c in codes
    )


def test_poor_overlap_with_fixed_clipping_discloses_trimmed_estimand():
    data = generate_synthetic(n=3000, scenario="poor_overlap", seed=11)
    result = run_ipw(
        data.x, data.a, data.y, overrides={"weights.clipping.enabled": True}
    )
    assert result.contract.clipping_enabled is True
    assert result.contract.clipping_profile != "none"
    clip_finding = [f for f in result.findings if f.code == "fixed_clipping_applied"]
    # Even if no score crossed the fixed bounds, the contract records the profile.
    assert "trimmed" in result.contract.estimand_note
    if result.overlap.n_clipped > 0:
        assert clip_finding and clip_finding[0].evidence["n_clipped"] > 0


def test_misspecified_model_is_flagged_by_calibration_on_large_sample():
    data = generate_synthetic(n=6000, scenario="misspecification", seed=33)
    result = run_ipw(data.x, data.a, data.y)
    codes = {f.code for f in result.findings}
    assert {"calibration_hl_reject", "calibration_hl_warn", "calibration_slope_warn"} & codes


def test_small_sample_marks_calibration_inconclusive_not_accepted():
    data = generate_synthetic(n=60, scenario="good_overlap", seed=4)
    result = run_ipw(data.x, data.a, data.y, overrides={"crossfit.n_splits": 2})
    cal = [f for f in result.findings if f.code == "calibration_sample_too_small"]
    assert cal and cal[0].status == "inconclusive"
    assert result.calibration["assessed"] is False


def test_att_and_atu_target_populations_differ_and_are_recorded():
    data = generate_synthetic(n=3000, scenario="good_overlap", seed=77)
    att = run_ipw(data.x, data.a, data.y, overrides={"estimand": "att"})
    atu = run_ipw(data.x, data.a, data.y, overrides={"estimand": "atu"})
    assert "TREATED" in att.contract.target_population
    assert "UNTREATED" in atu.contract.target_population
    # Heterogeneous effect tau(X)=2+0.5*X1 => ATT/ATU generally differ from ATE.
    assert att.contract.estimand == "att"
    assert atu.contract.estimand == "atu"


def test_request_id_propagates_and_result_is_json_serializable():
    data = generate_synthetic(n=500, scenario="good_overlap", seed=5)
    result = run_ipw(data.x, data.a, data.y, request_id="trace-xyz")
    assert result.request_id == "trace-xyz"
    js = result.to_json()
    assert "trace-xyz" in js and "NaN" not in js
