"""Tests for object-identity alignment and treatment-path discipline."""
from __future__ import annotations

import pytest

from app.contracts.models import (
    BalanceStrategy,
    FailureCategory,
    Observation as O,
    WeightPolicy,
)
from app.core.alignment import align_panel, classify_two_by_two
from app.core.errors import EstimationError


def obs(uid, t, y, treated=False, w=1.0):
    return O(unit_id=uid, period=t, y=y, treated=treated, weight=w)


def test_missing_period_is_excluded_not_zeroed():
    # m1 appears only in the post period. Imputing pre=0 would fabricate a
    # change; correct behavior is identity alignment plus an exclusion record.
    data = [
        obs("t1", 0, 2.0), obs("t1", 1, 5.0, True),
        obs("c1", 0, 1.0), obs("c1", 1, 1.0),
        obs("m1", 1, 20.0, True),
    ]
    panel = align_panel(
        data,
        balance=BalanceStrategy.BALANCED,
        weight_policy=WeightPolicy.UNIT_FIXED,
    )
    assert "m1" not in panel.balanced_units
    rec = next(e for e in panel.excluded if e.unit_id == "m1")
    assert rec.reason is FailureCategory.UNBALANCED_PANEL
    assert "NOT imputed as zero" in rec.detail
    assert panel.balanced_units == ["c1", "t1"]


def test_duplicate_cell_is_rejected():
    data = [obs("u1", 0, 1.0), obs("u1", 0, 2.0), obs("u1", 1, 3.0, True)]
    with pytest.raises(EstimationError) as exc:
        align_panel(data, balance=BalanceStrategy.BALANCED, weight_policy=WeightPolicy.NONE)
    assert exc.value.category is FailureCategory.DUPLICATE_UNIT_PERIOD


def test_empty_and_single_period_categories():
    with pytest.raises(EstimationError) as exc:
        align_panel([], balance=BalanceStrategy.BALANCED, weight_policy=WeightPolicy.NONE)
    assert exc.value.category is FailureCategory.EMPTY_PANEL

    with pytest.raises(EstimationError) as exc:
        align_panel([obs("u", 0, 1.0)], balance=BalanceStrategy.BALANCED,
                    weight_policy=WeightPolicy.NONE)
    assert exc.value.category is FailureCategory.SINGLE_PERIOD


def test_requested_periods_must_be_observed_and_ordered():
    data = [obs("u", 0, 1.0), obs("u", 1, 2.0)]
    with pytest.raises(EstimationError) as exc:
        align_panel(data, balance=BalanceStrategy.BALANCED, weight_policy=WeightPolicy.NONE,
                    pre_period=0, post_period=5)
    assert exc.value.category is FailureCategory.NOT_TWO_PERIODS
    with pytest.raises(EstimationError) as exc:
        align_panel(data, balance=BalanceStrategy.BALANCED, weight_policy=WeightPolicy.NONE,
                    pre_period=1, post_period=0)
    assert exc.value.category is FailureCategory.INVALID_REQUEST


def test_treatment_reversal_and_always_treated_excluded():
    data = [
        obs("rev", 0, 1.0, True), obs("rev", 1, 1.0, False),
        obs("alw", 0, 1.0, True), obs("alw", 1, 2.0, True),
        obs("sw", 0, 1.0, False), obs("sw", 1, 4.0, True),
        obs("ctl", 0, 1.0), obs("ctl", 1, 1.0),
    ]
    panel = align_panel(data, balance=BalanceStrategy.BALANCED, weight_policy=WeightPolicy.NONE)
    treated, control, excluded, _ = classify_two_by_two(panel)
    assert treated == ["sw"]
    assert control == ["ctl"]
    reasons = {e.unit_id: e.reason for e in excluded}
    assert reasons["rev"] is FailureCategory.INCONSISTENT_TREATMENT_PATH
    assert reasons["alw"] is FailureCategory.INCONSISTENT_TREATMENT_PATH


def test_weight_frozen_at_baseline_and_invalid_weight_excluded():
    data = [
        obs("t1", 0, 0.0, w=3.0), obs("t1", 1, 9.0, True, w=99.0),
        obs("c1", 0, 0.0, w=1.0), obs("c1", 1, 1.0, w=1.0),
        obs("c2", 0, 0.0, w=0.0), obs("c2", 1, 1.0, w=0.0),
    ]
    panel = align_panel(data, balance=BalanceStrategy.BALANCED,
                        weight_policy=WeightPolicy.UNIT_FIXED)
    # post-period weight 99 must be ignored: the baseline 3 is frozen.
    assert panel.units["t1"].fixed_weight == 3.0
    bad = [e for e in panel.excluded if e.unit_id == "c2"]
    assert bad and bad[0].reason is FailureCategory.INVALID_WEIGHT


def test_no_balanced_units_raises():
    data = [obs("a", 0, 1.0), obs("b", 1, 2.0, True)]
    with pytest.raises(EstimationError) as exc:
        align_panel(data, balance=BalanceStrategy.BALANCED, weight_policy=WeightPolicy.NONE)
    assert exc.value.category is FailureCategory.NO_ESTIMABLE_UNITS
