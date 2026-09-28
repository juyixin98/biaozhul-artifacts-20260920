"""Segmentation plan and segmented-vs-unsegmented consistency tests."""
import numpy as np
import pytest

from krylov_expm.config import SolverConfig
from krylov_expm.propagator import plan_segment_count, propagate

from conftest import load_fixture


def test_segmentation_plan_rule_is_fixed():
    cfg = SolverConfig(segment_theta=1.0)
    assert plan_segment_count(2.0, 3.0, cfg) == 6
    assert plan_segment_count(-2.0, 3.0, cfg) == 6
    assert plan_segment_count(0.0, 3.0, cfg) == 1
    assert plan_segment_count(1.0, 0.0, cfg) == 1
    capped = SolverConfig(segment_theta=1.0, max_segments=5)
    assert plan_segment_count(100.0, 3.0, capped) == 5


def test_segmented_and_unsegmented_results_agree():
    A, v = load_fixture("grcar_nonnormal")
    t, tol = 2.4, 1e-10
    one = SolverConfig(segment_theta=1.0e9)
    many = SolverConfig(segment_theta=0.5)
    w_one, ev_one = propagate(A, v, t, tol, one)
    w_many, ev_many = propagate(A, v, t, tol, many)
    assert ev_one.planned_segments == 1
    assert ev_many.planned_segments > 4
    rel = np.linalg.norm(w_one - w_many) / np.linalg.norm(w_one)
    assert rel < 1e-8


def test_cumulative_error_is_sum_of_segment_estimates():
    A, v = load_fixture("symmetric_tridiag")
    cfg = SolverConfig(segment_theta=0.25)
    _, evidence = propagate(A, v, 1.5, 1e-9, cfg)
    total = sum(seg.error_estimate for seg in evidence.segments)
    assert evidence.cumulative_error_estimate == pytest.approx(total)
    assert len(evidence.segments) == evidence.planned_segments
    assert all(seg.subspace_residual_norm >= 0.0 for seg in evidence.segments)
    assert all(seg.converged for seg in evidence.segments)


def test_negative_time_segmentation_uses_absolute_value():
    A, v = load_fixture("symmetric_tridiag")
    cfg = SolverConfig(segment_theta=1.0)
    _, ev_pos = propagate(A, v, 1.5, 1e-9, cfg)
    _, ev_neg = propagate(A, v, -1.5, 1e-9, cfg)
    assert ev_pos.planned_segments == ev_neg.planned_segments
