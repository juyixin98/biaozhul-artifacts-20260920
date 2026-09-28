"""Estimation tests against hand-computed answers and an independent oracle.

These tests do not merely check "the endpoint runs". They assert exact point
estimates, the difference decomposition, the analytically derived clustered
standard error, and cross-validate the core against ``tests/reference.py``
(which never imports the core) and the paper constants in
``tests/reference_expected.py``.
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from app.contracts.models import (
    BalanceStrategy,
    ControlGroup,
    FailureCategory,
    Observation as O,
    WeightPolicy,
)
from app.core.alignment import align_panel, classify_two_by_two
from app.core.errors import EstimationError
from app.core.estimation import (
    build_event_support,
    did_first_difference_regression,
    event_study_twfe,
    four_cell_decomposition,
)
from app.reproducibility.fixtures import build_fixture
from tests import reference as ref
from tests import reference_expected as expected


def _panel(obs, **kw):
    return align_panel(
        obs,
        balance=kw.get("balance", BalanceStrategy.BALANCED),
        weight_policy=kw.get("weight_policy", WeightPolicy.UNIT_FIXED),
        pre_period=kw.get("pre"),
        post_period=kw.get("post"),
    )


# --------------------------------------------------------------------------- #
# hand_2x2: exact four-cell means, DID, and clustered SE
# --------------------------------------------------------------------------- #
def test_hand_2x2_four_cell_decomposition():
    panel = _panel(build_fixture("hand_2x2"))
    treated, control, _, _ = classify_two_by_two(panel)
    four = four_cell_decomposition(panel, treated, control)

    assert four.treat_pre == expected.HAND_2X2["treat_pre"]
    assert four.treat_post == expected.HAND_2X2["treat_post"]
    assert four.control_pre == expected.HAND_2X2["control_pre"]
    assert four.control_post == expected.HAND_2X2["control_post"]
    assert four.treat_change == expected.HAND_2X2["treat_change"]
    assert four.control_change == expected.HAND_2X2["control_change"]
    assert four.did == pytest.approx(expected.HAND_2X2["did"], abs=1e-12)
    assert four.n_treat == expected.HAND_2X2["n_treat"]
    assert four.n_control == expected.HAND_2X2["n_control"]


def test_hand_2x2_matches_independent_reference():
    obs = build_fixture("hand_2x2")
    panel = _panel(obs)
    treated, control, _, _ = classify_two_by_two(panel)

    # Build the oracle inputs straight from the fixture, bypassing the core.
    def yw(uids, t):
        return [(next(o.y for o in obs if o.unit_id == u and o.period == t), 1.0) for u in uids]

    oracle = ref.ref_2x2_did(yw(treated, 0), yw(treated, 1), yw(control, 0), yw(control, 1))
    four = four_cell_decomposition(panel, treated, control)
    for key in ("treat_pre", "treat_post", "control_pre", "control_post",
                "treat_change", "control_change", "did"):
        assert getattr(four, key) == pytest.approx(oracle[key], abs=1e-12)


def test_first_difference_coefficient_equals_four_cell_did():
    panel = _panel(build_fixture("hand_2x2"))
    treated, control, _, _ = classify_two_by_two(panel)
    four = four_cell_decomposition(panel, treated, control)
    ols, dy, t_ind, w, clusters = did_first_difference_regression(panel, treated, control)
    # The two estimators of the same estimand must agree to machine precision.
    assert ols.beta[1] == pytest.approx(four.did, abs=1e-12)
    # Intercept is the control mean change.
    assert ols.beta[0] == pytest.approx(expected.HAND_2X2["control_change"], abs=1e-12)


def test_clustered_standard_error_is_hand_derived_value():
    panel = _panel(build_fixture("hand_2x2"))
    treated, control, _, _ = classify_two_by_two(panel)
    ols, dy, t_ind, w, clusters = did_first_difference_regression(panel, treated, control)

    # Paper value (see reference_expected.py): se = sqrt(2).
    assert ols.se[1] == pytest.approx(expected.HAND_2X2["se"], abs=1e-10)
    assert ols.n_clusters == 4

    # And the independent sandwich implementation agrees.
    oracle = ref.ref_fd_did(list(dy), list(map(int, t_ind)), list(w), list(clusters))
    assert oracle["did"] == pytest.approx(expected.HAND_2X2["did"], abs=1e-12)
    assert oracle["se"] == pytest.approx(expected.HAND_2X2["se"], abs=1e-10)


def test_cluster_se_differs_from_naive_ols_and_clustering_matters():
    # In a 2x2 first difference each unit cluster contributes exactly one
    # residual, so unit-clustered CRV1 coincides with HC1 heteroskedasticity-
    # robust SE (serial correlation is already removed by differencing). It
    # still differs from the homoskedastic ("naive") SE when group sizes and
    # residual variances are asymmetric. Construct that case:
    #   3 treated units, identical change 4 (residual 0)
    #   2 controls, changes 0 and 4 (mean 2, residuals -2,+2)
    #   b = 4 - 2 = 2.
    data = [
        O(unit_id="t1", period=0, y=0.0, treated=False),
        O(unit_id="t1", period=1, y=4.0, treated=True),
        O(unit_id="t2", period=0, y=0.0, treated=False),
        O(unit_id="t2", period=1, y=4.0, treated=True),
        O(unit_id="t3", period=0, y=0.0, treated=False),
        O(unit_id="t3", period=1, y=4.0, treated=True),
        O(unit_id="c1", period=0, y=0.0, treated=False),
        O(unit_id="c1", period=1, y=0.0, treated=False),
        O(unit_id="c2", period=0, y=0.0, treated=False),
        O(unit_id="c2", period=1, y=4.0, treated=False),
    ]
    panel = _panel(data)
    treated, control, _, _ = classify_two_by_two(panel)
    ols, dy, t_ind, _, _ = did_first_difference_regression(panel, treated, control)
    assert ols.beta[1] == pytest.approx(2.0, abs=1e-12)
    x = np.column_stack([np.ones(len(dy)), t_ind])
    beta, *_ = np.linalg.lstsq(x, dy, rcond=None)
    r = dy - x @ beta
    sigma2 = (r @ r) / (len(dy) - 2)
    naive_se = math.sqrt(sigma2 * np.linalg.inv(x.T @ x)[1, 1])
    # Paper values: clustered(HC1) se^2 = 10/3; naive homoskedastic se^2 = 20/9.
    assert ols.se[1] ** 2 == pytest.approx(10.0 / 3.0, abs=1e-9)
    assert naive_se ** 2 == pytest.approx(20.0 / 9.0, abs=1e-9)
    assert ols.se[1] != pytest.approx(naive_se, rel=1e-6)


# --------------------------------------------------------------------------- #
# missing-period fixture: exclusion, not zeroing
# --------------------------------------------------------------------------- #
def test_missing_period_units_excluded_and_estimate_unchanged():
    panel = _panel(build_fixture("missing_period"))
    excluded_ids = {e.unit_id for e in panel.excluded
                    if e.reason is FailureCategory.UNBALANCED_PANEL}
    assert excluded_ids == expected.MISSING_PERIOD_EXCLUDED
    treated, control, _, _ = classify_two_by_two(panel)
    four = four_cell_decomposition(panel, treated, control)
    assert four.did == pytest.approx(expected.MISSING_PERIOD_DID, abs=1e-12)


# --------------------------------------------------------------------------- #
# weighted fixture: baseline-fixed weights
# --------------------------------------------------------------------------- #
def test_weighted_did_uses_frozen_baseline_weights():
    obs = build_fixture("weighted_2x2")
    panel = _panel(obs, weight_policy=WeightPolicy.UNIT_FIXED)
    treated, control, _, _ = classify_two_by_two(panel)
    four = four_cell_decomposition(panel, treated, control)
    assert four.did == pytest.approx(expected.WEIGHTED_DID, abs=1e-12)

    # Independent reference using the exact (y, baseline-weight) pairs.
    def cell(uids, t):
        return [(next(o.y for o in obs if o.unit_id == u and o.period == t),
                 next(o.weight for o in obs if o.unit_id == u and o.period == 0))
                for u in uids]
    oracle = ref.ref_2x2_did(cell(treated, 0), cell(treated, 1),
                             cell(control, 0), cell(control, 1))
    assert four.did == pytest.approx(oracle["did"], abs=1e-12)


# --------------------------------------------------------------------------- #
# contamination
# --------------------------------------------------------------------------- #
def test_contaminated_control_excluded_clean_did():
    panel = _panel(build_fixture("contaminated_control"), pre=0, post=2)
    treated, control, excluded, _ = classify_two_by_two(panel)
    assert expected.CONTAMINATED_UNIT not in control
    assert any(e.unit_id == expected.CONTAMINATED_UNIT
               and e.reason is FailureCategory.CONTROL_GROUP_CONTAMINATED for e in excluded)
    assert control == ["c1"]
    four = four_cell_decomposition(panel, treated, control)
    assert four.did == pytest.approx(expected.CONTAMINATED_CLEAN_DID, abs=1e-12)
    assert four.n_control == expected.CONTAMINATED_CLEAN_N_CONTROL


# --------------------------------------------------------------------------- #
# single-group / no-control failure categories
# --------------------------------------------------------------------------- #
def test_only_one_group_is_rejected_with_category():
    data = build_fixture("hand_2x2")
    treated_only = [o for o in data if o.unit_id.startswith("t")]
    panel = _panel(treated_only)
    treated, control, _, _ = classify_two_by_two(panel)
    with pytest.raises(EstimationError) as exc:
        four_cell_decomposition(panel, treated, control)
    assert exc.value.category is FailureCategory.SINGLE_GROUP


def test_no_clean_control_reports_dedicated_category():
    # Both candidate controls are ever-treated -> NO_VALID_CONTROL_GROUP.
    data = [
        o for o in build_fixture("contaminated_control") if o.unit_id != "c1"
    ]
    panel = _panel(data, pre=0, post=2)
    treated, control, excluded, _ = classify_two_by_two(panel)
    assert control == []
    n_contam = sum(1 for e in excluded if e.reason is FailureCategory.CONTROL_GROUP_CONTAMINATED)
    with pytest.raises(EstimationError) as exc:
        four_cell_decomposition(panel, treated, control, n_contaminated=n_contam)
    assert exc.value.category is FailureCategory.NO_VALID_CONTROL_GROUP


# --------------------------------------------------------------------------- #
# event study: point estimates + explicit out-of-support refusal
# --------------------------------------------------------------------------- #
def test_event_study_recovers_known_coefficients_and_matches_reference():
    obs = build_fixture("staggered_events")
    panel = _panel(obs)
    support = build_event_support(
        panel.units,
        control_group=ControlGroup.NEVER_TREATED,
        requested_min=-2,
        requested_max=1,
    )
    assert support.window == expected.EVENT_WINDOW
    ols, event_cols, rows, all_uids, all_periods = event_study_twfe(
        panel.units, support, weight_policy=WeightPolicy.UNIT_FIXED
    )
    core_est = {k: float(ols.beta[event_cols.index(k)]) for k in event_cols}
    for k, tau in expected.EVENT_TAU.items():
        if k == -1:
            continue  # normalized reference, not a coefficient
        assert core_est[k] == pytest.approx(tau, abs=1e-9)

    # Independent oracle along a different parametrization/code path.
    oracle = ref.ref_event_study(rows, all_uids, all_periods, support.window)
    for k, v in oracle.items():
        assert core_est[k] == pytest.approx(v, abs=1e-9)


def test_event_study_refuses_out_of_support():
    obs = build_fixture("staggered_events_out_of_support")
    panel = _panel(obs)
    with pytest.raises(EstimationError) as exc:
        build_event_support(
            panel.units,
            control_group=ControlGroup.NEVER_TREATED,
            requested_min=-2,
            requested_max=expected.EVENT_OUT_OF_SUPPORT_K,
        )
    assert exc.value.category is FailureCategory.OUT_OF_SUPPORT_EVENT_TIME
    assert "2" in exc.value.message


def test_event_study_requires_treated_cohort():
    # Only never-treated units -> SINGLE_GROUP.
    obs = [o for o in build_fixture("staggered_events") if o.unit_id.startswith("n")]
    panel = _panel(obs)
    with pytest.raises(EstimationError) as exc:
        build_event_support(panel.units, control_group=ControlGroup.NEVER_TREATED,
                            requested_min=-1, requested_max=1)
    assert exc.value.category is FailureCategory.SINGLE_GROUP


def test_event_study_strict_needs_never_treated():
    obs = [o for o in build_fixture("staggered_events") if not o.unit_id.startswith("n")]
    panel = _panel(obs)
    with pytest.raises(EstimationError) as exc:
        build_event_support(panel.units, control_group=ControlGroup.NEVER_TREATED,
                            requested_min=-1, requested_max=1)
    assert exc.value.category is FailureCategory.NO_VALID_CONTROL_GROUP
