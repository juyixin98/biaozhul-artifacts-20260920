"""Acceptance tests: planner behavior on the three benchmark scenarios."""

import math

import pytest

from hybrid_astar import GridMap, HybridAStarPlanner, PlannerConfig, Vehicle
from hybrid_astar.scenarios import narrow_corridor, reverse_uturn, unsolvable
from hybrid_astar.validate import (
    check_collision_free,
    check_gears,
    check_kinematics,
)


def make_planner(scenario, **cfg_overrides):
    grid = GridMap(scenario.grid, resolution=scenario.resolution)
    vehicle = Vehicle()
    cfg = PlannerConfig(**cfg_overrides)
    return HybridAStarPlanner(grid, vehicle, cfg), vehicle


def test_narrow_corridor_path_valid():
    sc = narrow_corridor()
    planner, vehicle = make_planner(sc)
    res = planner.plan(sc.start, sc.goal)
    assert res.success, res.message
    # reaches the goal region
    assert math.hypot(res.path_x[-1] - sc.goal[0],
                      res.path_y[-1] - sc.goal[1]) <= planner.config.goal_tol_xy
    # independent brute-force collision check along the dense path
    assert check_collision_free(sc.grid, sc.resolution, vehicle,
                                res.path_x, res.path_y, res.path_theta)
    # curvature never exceeds 1 / min_turning_radius
    ok, kmax = check_kinematics(vehicle, res.path_x, res.path_y, res.path_theta)
    assert ok, f"curvature {kmax} exceeds {vehicle.kappa_max}"


def test_zero_heuristic_baseline_same_cost_more_expansions():
    sc = narrow_corridor()
    planner_h, _ = make_planner(sc)
    planner_0, _ = make_planner(sc, use_heuristic=False)
    res_h = planner_h.plan(sc.start, sc.goal)
    res_0 = planner_0.plan(sc.start, sc.goal)
    assert res_h.success and res_0.success
    # both are optimal up to the lattice/goal-tolerance discretization
    assert res_0.cost == pytest.approx(res_h.cost, rel=0.05)
    # the informed heuristic must not expand more states than the baseline
    assert res_h.expansions <= res_0.expansions


def test_reverse_required_for_dead_end_uturn():
    sc = reverse_uturn()
    # forward-only cannot escape the dead-end alley
    planner_fwd, _ = make_planner(sc, allow_reverse=False)
    res_fwd = planner_fwd.plan(sc.start, sc.goal)
    assert not res_fwd.success
    # with reverse enabled the planner backs out and reaches the goal
    planner_rev, vehicle = make_planner(sc, allow_reverse=True)
    res = planner_rev.plan(sc.start, sc.goal)
    assert res.success, res.message
    has_fwd, has_rev = check_gears(res.path_gear)
    assert has_rev, "expected the path to contain reverse segments"
    assert has_fwd
    assert check_collision_free(sc.grid, sc.resolution, vehicle,
                                res.path_x, res.path_y, res.path_theta)
    ok, _ = check_kinematics(vehicle, res.path_x, res.path_y, res.path_theta)
    assert ok


def test_unsolvable_map_reports_failure():
    sc = unsolvable()
    planner, _ = make_planner(sc)
    res = planner.plan(sc.start, sc.goal)
    assert not res.success
    assert "no path" in res.message or "collision" in res.message


def test_start_in_collision_rejected():
    sc = narrow_corridor()
    planner, _ = make_planner(sc)
    # (0.5, 10.0) sits on the horizontal wall row
    res = planner.plan((0.5, 10.0, 0.0), sc.goal)
    assert not res.success
    assert "start" in res.message
