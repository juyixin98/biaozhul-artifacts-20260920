"""Tests for the committed group-sequential (interim) machinery.

Constants are hand-derived / from published tables (Pocock 1977, O'Brien-
Fleming 1979), and sizes are independently checked by Monte Carlo draws.
"""
from __future__ import annotations

import numpy as np
import pytest
from scipy.stats import norm

from sample_size_planner.contracts import FailureCategory
from sample_size_planner.diagnostics import interim as im


@pytest.mark.unit
def test_single_look_reduces_to_fixed_critical_value() -> None:
    sched = im.build_schedule([1.0], alpha=0.05, two_sided=False)
    assert sched.family is im.BoundaryFamily.FIXED
    assert sched.z_boundaries[0] == pytest.approx(norm.isf(0.05), abs=1e-9)
    # and the size is exactly alpha
    assert im._first_cross_probability(np.array(sched.z_boundaries), np.array([1.0]), 0.0) == \
        pytest.approx(0.05, abs=1e-9)


@pytest.mark.unit
def test_pocock_two_look_one_sided_constant() -> None:
    sched = im.build_schedule([0.5, 1.0], alpha=0.05, two_sided=False,
                              family=im.BoundaryFamily.POCOCK)
    # constant boundary ~1.875 at one-sided alpha .05 (table value)
    assert sched.z_boundaries[0] == pytest.approx(sched.z_boundaries[1], abs=1e-9)
    assert sched.z_boundaries[0] == pytest.approx(1.875, abs=2e-3)
    size = im._first_cross_probability(np.array(sched.z_boundaries), np.array([0.5, 1.0]), 0.0)
    assert size == pytest.approx(0.05, abs=1e-5)


@pytest.mark.unit
def test_pocock_two_sided_matches_published_constant() -> None:
    sched = im.build_schedule([0.5, 1.0], alpha=0.05, two_sided=True,
                              family=im.BoundaryFamily.POCOCK)
    # published Pocock two-sided alpha=.05, K=2 constant
    assert sched.z_boundaries[0] == pytest.approx(2.178, abs=2e-3)


@pytest.mark.unit
def test_obrien_fleming_two_sided_table_values() -> None:
    sched = im.build_schedule([0.25, 0.5, 0.75, 1.0], alpha=0.05, two_sided=True,
                              family=im.BoundaryFamily.OBRIEN_FLEMING)
    published = [4.049, 2.863, 2.337, 2.024]
    assert list(sched.z_boundaries) == pytest.approx(published, abs=8e-3)
    # early O-F boundaries are much stricter than Pocock
    assert sched.z_boundaries[0] > 3.9


@pytest.mark.unit
def test_boundary_size_independently_checked_by_monte_carlo() -> None:
    rng = np.random.default_rng(1234)
    times = np.array([0.25, 0.5, 0.75, 1.0])
    sched = im.build_schedule(list(times), alpha=0.05, two_sided=False,
                              family=im.BoundaryFamily.OBRIEN_FLEMING)
    b = np.array(sched.z_boundaries)
    gaps = np.diff(np.concatenate(([0.0], times)))
    reps = 300_000
    inc = rng.normal(0.0, np.sqrt(gaps), size=(reps, 4))
    z = np.cumsum(inc, axis=1) / np.sqrt(times)
    mc_size = float((z >= b).any(axis=1).mean())
    analytic = im._first_cross_probability(b, times, 0.0)
    assert abs(mc_size - analytic) < 4 * np.sqrt(0.05 * 0.95 / reps)
    assert analytic == pytest.approx(0.05, abs=1e-4)


@pytest.mark.unit
def test_unplanned_look_is_rejected_outside_commitment() -> None:
    sched = im.build_schedule([0.5, 1.0], alpha=0.05, two_sided=False,
                              family=im.BoundaryFamily.POCOCK)
    im.verify_look_is_committed(sched, 0.5)   # committed look is fine
    with pytest.raises(im.InterimError) as exc:
        im.verify_look_is_committed(sched, 0.7)  # ad-hoc peek is not
    assert exc.value.category is FailureCategory.INTERIM_LOOK_OUTSIDE_COMMITMENT


@pytest.mark.unit
def test_schedule_must_end_at_final_analysis() -> None:
    with pytest.raises(im.InterimError) as exc:
        im.build_schedule([0.5, 0.8], alpha=0.05, two_sided=False)
    assert exc.value.category is FailureCategory.INTERIM_LOOK_OUTSIDE_COMMITMENT


@pytest.mark.unit
def test_too_many_looks_is_rejected() -> None:
    with pytest.raises(im.InterimError) as exc:
        im.build_schedule(list(np.linspace(0.1, 1.0, 21)), alpha=0.05,
                          two_sided=False, max_looks=20)
    assert exc.value.category is FailureCategory.TOO_MANY_INTERIM_LOOKS


@pytest.mark.unit
def test_non_increasing_times_rejected() -> None:
    with pytest.raises(im.InterimError) as exc:
        im.build_schedule([0.5, 0.5, 1.0], alpha=0.05, two_sided=False)
    assert exc.value.category is FailureCategory.INVALID_INPUT


@pytest.mark.unit
def test_interim_power_is_at_or_below_fixed_sample_power() -> None:
    plan = im.plan_interim([0.5, 1.0], alpha=0.05, drift_at_full_information=2.8,
                           two_sided=False, family=im.BoundaryFamily.POCOCK)
    assert plan.alternative_power <= plan.fixed_sample_power + 1e-9
    assert plan.power_loss_vs_fixed >= 0.0
    # cumulative alpha spent is increasing and lands on alpha
    assert plan.cumulative_alpha_spent[-1] == pytest.approx(0.05, abs=1e-4)
    assert all(a <= b + 1e-9 for a, b in zip(plan.cumulative_alpha_spent,
                                             plan.cumulative_alpha_spent[1:]))
