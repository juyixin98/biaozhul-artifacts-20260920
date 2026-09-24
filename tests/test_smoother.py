"""End-to-end solver behavior on the required fixture classes."""
import json
from pathlib import Path

import numpy as np
import pytest

from app.geometry import Rect
from app.smoother import MAX_POINTS_HARD_LIMIT, SmoothConfig, smooth_path

from conftest import dense_clearance

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def test_narrow_corridor_smooths_and_stays_clear(narrow_corridor):
    P, R, cfg = narrow_corridor
    res = smooth_path(P, R, cfg)
    assert res.status == "optimal"
    assert res.converged
    assert res.iterations <= cfg.max_iterations
    assert res.iterations > 0
    assert res.n_variables == 2 * (len(P) - 2)

    obs = res.residuals["observed_physical"]
    assert obs["max_deviation"] <= cfg.corridor + 1e-6
    assert obs["max_curvature"] <= cfg.curvature_cap + 1e-6
    assert obs["endpoint_error"] == pytest.approx(0.0, abs=1e-8)
    # fixed endpoints byte-for-byte
    assert np.allclose(res.points[0], P[0])
    assert np.allclose(res.points[-1], P[-1])

    # collision verification over the WHOLE dense trajectory
    assert res.verification["ok"]
    assert res.verification["samples_checked"] > len(P)
    assert res.verification["min_clearance"] >= cfg.clearance - 1e-9
    # independent re-check, not the service's own gate
    assert dense_clearance(res.points, R, res.verification["sample_spacing"]) >= cfg.clearance - 1e-9

    # smoothing actually reduced curvature variation vs. the input
    assert res.objective_components["jerk_sum_sq"] < 0.5


def test_corner_cut_does_not_take_illegal_chord(corner_cut):
    P, R, cfg = corner_cut
    # sanity: the straight chord from start to goal penetrates the block
    chord_pen = dense_clearance(np.array([P[0], P[-1]]), R, 0.2)
    assert chord_pen < 0

    res = smooth_path(P, R, cfg)
    assert res.status == "optimal"
    assert res.verification["ok"]
    assert res.verification["min_clearance"] >= cfg.clearance - 1e-9
    assert dense_clearance(res.points, R, res.verification["sample_spacing"]) >= cfg.clearance - 1e-9
    assert res.residuals["observed_physical"]["max_deviation"] <= cfg.corridor + 1e-6


def test_collision_hidden_between_control_points_is_rejected():
    # Sparse control points whose straight connecting segment passes through
    # an obstacle while every vertex is outside.
    P = np.array([[-5.0, 0.0], [0.0, -4.0], [0.0, 4.0], [5.0, 0.0]])
    R = [Rect(-1.5, -1.5, 1.5, 1.5)]
    cfg = SmoothConfig(corridor=8.0, clearance=0.0, max_iterations=50)
    res = smooth_path(P, R, cfg)
    # either feasible and verified collision free, or failure + original path
    if res.status == "optimal":
        assert res.verification["ok"]
        assert dense_clearance(res.points, R, 0.05) >= -1e-9
    else:
        assert np.allclose(res.points, P)


def test_repeated_consecutive_points_are_handled():
    P = np.array([(0, 0), (0, 0), (2, 0.3), (2, 0.3), (4, -0.3),
                  (6, 0.3), (8, 0), (8, 0)], float)
    res = smooth_path(P, [], SmoothConfig(curvature_cap=0.8, corridor=0.5))
    assert res.status == "optimal"
    assert res.collapsed_duplicates == 3
    assert len(res.points) == len(P)           # output indexing preserved
    assert np.allclose(res.points[0], res.points[1])
    assert np.allclose(res.points[-1], res.points[-2])
    assert np.allclose(res.points[2], res.points[3])


def test_nonconsecutive_revisit_is_preserved():
    P = np.array([[0.0, 0], [2.0, 0], [0.0, 0]], float)
    res = smooth_path(P, [], SmoothConfig(corridor=1.0))
    assert len(res.points) == 3
    assert np.allclose(res.points[0], res.points[2])  # loop closes


def test_all_identical_points_is_invalid_input():
    P = np.zeros((4, 2))
    res = smooth_path(P, [], SmoothConfig())
    assert res.status == "invalid_input"
    assert np.allclose(res.points, P)


def test_iteration_budget_is_honored(narrow_corridor):
    P, R, cfg = narrow_corridor
    cfg.max_iterations = 1
    res = smooth_path(P, R, cfg)
    assert res.iterations <= 1
    # on failure the published path must be the original
    assert np.allclose(res.points, P)
    assert res.status in {"iteration_limit", "infeasible", "optimal"}


def test_infeasible_rounding_returns_original_with_failure():
    P = np.array([(0, 0), (4, 0), (4, 4), (8, 4)], float)
    R = [Rect.from_center_wh(2, 2, 4, 4)]
    res = smooth_path(P, R, SmoothConfig(curvature_cap=0.05, corridor=0.5,
                                         clearance=0.0, max_iterations=300))
    assert res.status in {"infeasible", "iteration_limit"}
    assert res.converged is False
    assert np.allclose(res.points, P)              # no illegal path published
    assert res.residuals["max_violation"] < 0 or res.iterations >= 300


def test_input_that_already_collides_is_rejected():
    P = np.array([(0, 0), (5, 0), (5, 10), (10, 10)], float)
    R = [Rect.from_center_wh(5, 5, 4, 4)]
    res = smooth_path(P, R, SmoothConfig(clearance=0.05))
    assert res.status == "input_collision"
    assert np.allclose(res.points, P)
    assert res.verification["ok"] is False


def test_timeout_returns_failure_and_original():
    rng = np.random.default_rng(0)
    P = rng.normal(size=(80, 2)).cumsum(axis=0) * 0.1
    res = smooth_path(P, [], SmoothConfig(max_iterations=10_000, timeout_seconds=0.0005))
    assert res.status == "timeout"
    assert np.allclose(res.points, P)


def test_scale_invariance(narrow_corridor):
    P, R, cfg = narrow_corridor
    base = smooth_path(P, R, cfg)
    assert base.status == "optimal"
    for s in (1000.0, 0.001):
        Ps = P * s
        Rs = [Rect.from_center_wh((r.xmin + r.xmax) / 2 * s,
                                  (r.ymin + r.ymax) / 2 * s,
                                  (r.xmax - r.xmin) * s,
                                  (r.ymax - r.ymin) * s) for r in R]
        cfgs = SmoothConfig(
            curvature_cap=cfg.curvature_cap / s,
            corridor=cfg.corridor * s,
            clearance=cfg.clearance * s,
            max_iterations=200,
        )
        r = smooth_path(Ps, Rs, cfgs)
        assert r.status == "optimal"
        assert np.allclose(r.points / s, base.points, atol=1e-7 * max(1.0, s))
        assert r.iterations == base.iterations
        assert r.verification["min_clearance"] / s == pytest.approx(
            base.verification["min_clearance"], abs=1e-9)


def test_residuals_are_recorded_on_success(narrow_corridor):
    P, R, cfg = narrow_corridor
    res = smooth_path(P, R, cfg)
    assert res.residuals["satisfied"]
    for key in ("deviation", "curvature", "min_edge_length", "clearance",
                "fixed_endpoints"):
        assert key in res.residuals["residuals_physical"]
        assert res.residuals["residuals_physical"][key] >= -1e-7
    assert "objective_initial" in res.objective_components
    assert res.objective <= res.objective_components["objective_initial"] + 1e-12


def test_two_point_path_is_verified_not_optimized():
    P = np.array([[0.0, 0], [3.0, 4.0]])
    res = smooth_path(P, [], SmoothConfig())
    assert res.status == "optimal"
    assert np.allclose(res.points, P)
    assert res.verification["samples_checked"] >= 2


def test_100_point_hard_limit_path_runs():
    doc = json.loads((EXAMPLES / "large_scale_mm.json").read_text())
    P = np.array(doc["path"], float)
    R = [Rect.from_center_wh(o["cx"], o["cy"], o["width"], o["height"])
         for o in doc["obstacles"]]
    p = doc["params"]
    cfg = SmoothConfig(curvature_cap=p["curvature_cap"], corridor=p["corridor"],
                       clearance=p["clearance"], max_iterations=p["max_iterations"],
                       timeout_seconds=p["timeout_seconds"])
    res = smooth_path(P, R, cfg)
    assert len(P) == MAX_POINTS_HARD_LIMIT
    assert res.status in {"optimal", "infeasible", "iteration_limit", "timeout"}
    if res.status == "optimal":
        assert res.verification["ok"]
        assert dense_clearance(res.points, R,
                               res.verification["sample_spacing"]) >= cfg.clearance - 1e-6
    else:
        assert np.allclose(res.points, P)
