"""D* Lite 增量规划测试: 与独立 Dijkstra 对拍 + 规定场景。"""

import math

import numpy as np
import pytest

from app.planner import DStarLite, GridView, dijkstra

INF = float("inf")
TOL = 1e-7


def uniform_view(h, w, conn=8, weight=1.0):
    return GridView(
        np.full((h, w), weight), np.zeros((h, w), dtype=bool), conn
    )


def assert_optimal(planner, view, start, goal):
    """成本与独立 Dijkstra 一致, 路径合法且成本一致。"""
    bench = dijkstra(view, start, goal)
    cost = planner.optimal_cost()
    if math.isinf(bench["cost"]):
        assert math.isinf(cost)
        assert planner.extract_path() is None
        return
    assert not math.isinf(cost)
    assert cost == pytest.approx(bench["cost"], abs=1e-6)
    path = planner.extract_path()
    assert path is not None
    assert path[0] == start and path[-1] == goal
    total = 0.0
    for a, z in zip(path, path[1:]):
        e = view.edge_cost(a, z)
        assert e is not None, f"非法步 {a}->{z}"
        total += e
    assert total == pytest.approx(bench["cost"], abs=1e-6)


def test_open_grid_optimal_diagonal_cost():
    view = uniform_view(5, 5, conn=8)
    p = DStarLite(view, (0, 0), (4, 4))
    # 4 斜 + 0 直 = 4*sqrt(2)
    assert p.optimal_cost() == pytest.approx(4 * math.sqrt(2.0))
    assert_optimal(p, view, (0, 0), (4, 4))


def test_connectivity4_open_grid_cost():
    view = uniform_view(4, 4, conn=4)
    p = DStarLite(view, (0, 0), (3, 3))
    assert p.optimal_cost() == pytest.approx(6.0)  # 曼哈顿
    assert_optimal(p, view, (0, 0), (3, 3))


def test_added_obstacle_detours():
    weights = np.ones((5, 5))
    blocked = np.zeros((5, 5), dtype=bool)
    view = GridView(weights, blocked, 8)
    p = DStarLite(view, (0, 0), (4, 4))
    before = p.optimal_cost()

    # 在对角走廊上放障碍 (2,2), 迫使绕路 => 成本上升
    blocked[2, 2] = True
    view2 = GridView(weights, blocked, 8)
    diag = p.update_grid(view2, [(2, 2)])
    after = p.optimal_cost()
    assert after > before
    assert_optimal(p, view2, (0, 0), (4, 4))
    # 诊断: 这是观测值而非承诺; 增量确实做了再扩展工作
    assert diag.expanded_nonstale >= 0
    assert diag.full_reset is False


def test_wall_makes_unreachable_then_removal_restores():
    weights = np.ones((5, 5))
    blocked = np.zeros((5, 5), dtype=bool)
    p = DStarLite(GridView(weights, blocked, 8), (0, 2), (4, 2))

    # 整行墙 => 不可达
    blocked[2, :] = True
    changed = [(2, c) for c in range(5)]
    view_b = GridView(weights, blocked, 8)
    p.update_grid(view_b, changed)
    assert math.isinf(p.optimal_cost())
    assert p.extract_path() is None

    # 开一个缺口 => 恢复可达且最优
    blocked[2, 0] = False
    view_c = GridView(weights, blocked, 8)
    p.update_grid(view_c, [(2, 0)])
    assert_optimal(p, view_c, (0, 2), (4, 2))


def test_obstacle_removal_shortens_path():
    weights = np.ones((6, 6))
    blocked = np.zeros((6, 6), dtype=bool)
    # 一道带缺口的墙, 初始必须绕远
    blocked[3, 0:5] = True
    blocked[3, 5] = False
    view_a = GridView(weights, blocked, 8)
    p = DStarLite(view_a, (0, 0), (5, 5))
    detour_cost = p.optimal_cost()
    assert_optimal(p, view_a, (0, 0), (5, 5))

    # 打开墙中央 => 成本下降
    blocked[3, 2] = False
    view_b = GridView(weights, blocked, 8)
    p.update_grid(view_b, [(3, 2)])
    assert p.optimal_cost() < detour_cost
    assert_optimal(p, view_b, (0, 0), (5, 5))


def test_move_start_incremental():
    weights = np.ones((6, 6))
    blocked = np.zeros((6, 6), dtype=bool)
    blocked[2, 1:5] = True
    view = GridView(weights, blocked, 8)
    p = DStarLite(view, (0, 0), (5, 0))
    assert_optimal(p, view, (0, 0), (5, 0))

    # 移动起点若干次, 每次仍与 Dijkstra 一致
    for ns in [(0, 5), (1, 0), (4, 5), (5, 5)]:
        p.move_start(ns)
        assert_optimal(p, view, ns, (5, 0))


def test_move_start_to_blocked_rejected():
    weights = np.ones((3, 3))
    blocked = np.zeros((3, 3), dtype=bool)
    blocked[0, 2] = True
    p = DStarLite(GridView(weights, blocked, 8), (0, 0), (2, 2))
    with pytest.raises(Exception):
        p.move_start((0, 2))


def test_start_goal_blocked_at_construction_rejected():
    weights = np.ones((3, 3))
    blocked = np.zeros((3, 3), dtype=bool)
    blocked[0, 0] = True
    from app.planner import PlannerError

    with pytest.raises(PlannerError):
        DStarLite(GridView(weights, blocked, 8), (0, 0), (2, 2))
    blocked[0, 0] = False
    blocked[2, 2] = True
    with pytest.raises(PlannerError):
        DStarLite(GridView(weights, blocked, 8), (0, 0), (2, 2))


def test_weight_update_changes_optimal_cost():
    weights = np.full((4, 4), 1.0)
    blocked = np.zeros((4, 4), dtype=bool)
    view = GridView(weights, blocked, 8)
    p = DStarLite(view, (0, 0), (3, 3))
    base = p.optimal_cost()

    # 抬高对角走廊附近单元权重, 最优路径应变贵或绕行
    weights[1, 1] = 100.0
    weights[2, 2] = 100.0
    view2 = GridView(weights, blocked, 8)
    p.update_grid(view2, [(1, 1), (2, 2)])
    assert p.optimal_cost() >= base
    assert_optimal(p, view2, (0, 0), (3, 3))

    # 恢复权重 => 回到原成本
    weights[1, 1] = 1.0
    weights[2, 2] = 1.0
    view3 = GridView(weights, blocked, 8)
    p.update_grid(view3, [(1, 1), (2, 2)])
    assert p.optimal_cost() == pytest.approx(base, abs=1e-6)
    assert_optimal(p, view3, (0, 0), (3, 3))


def test_floor_drop_triggers_full_reset_not_stale_reuse():
    weights = np.ones((5, 5))
    blocked = np.zeros((5, 5), dtype=bool)
    p = DStarLite(GridView(weights, blocked, 8), (0, 0), (4, 4))
    weights[2, 3] = 0.0
    view = GridView(weights, blocked, 8)
    diag = p.update_grid(view, [(2, 3)])
    assert diag.full_reset is True  # 启发式地板下降 => 显式完整重置
    assert_optimal(p, view, (0, 0), (4, 4))


def test_zero_weight_plateau_path():
    weights = np.zeros((4, 4))
    blocked = np.zeros((4, 4), dtype=bool)
    view = GridView(weights, blocked, 8)
    p = DStarLite(view, (0, 0), (3, 3))
    assert p.optimal_cost() == 0.0
    assert_optimal(p, view, (0, 0), (3, 3))


def test_dijkstra_independent_baseline_unreachable():
    weights = np.ones((3, 3))
    blocked = np.zeros((3, 3), dtype=bool)
    blocked[1, :] = True
    r = dijkstra(GridView(weights, blocked, 4), (0, 0), (2, 0))
    assert math.isinf(r["cost"])
    assert r["path"] is None
    assert r["expansions"] >= 3  # 搜遍上半部


def test_dijkstra_expansion_count_reported():
    view = uniform_view(10, 10, 8)
    r = dijkstra(view, (0, 0), (9, 9))
    assert r["expansions"] > 0
    assert r["path"][0] == (0, 0) and r["path"][-1] == (9, 9)


def test_diagnostics_are_diagnostic_not_promise():
    # 结构检查: 诊断字段存在且类型正确, 但不施加"必须更少扩展"的硬阈值
    weights = np.ones((8, 8))
    blocked = np.zeros((8, 8), dtype=bool)
    p = DStarLite(GridView(weights, blocked, 8), (0, 0), (7, 7))
    blocked[4, 4] = True
    diag = p.update_grid(GridView(weights, blocked, 8), [(4, 4)])
    d = diag.to_dict()
    for key in (
        "heap_pops",
        "expanded_nonstale",
        "stale_key_reinserts",
        "rhs_recomputations",
        "full_reset",
        "reason",
    ):
        assert key in d
    assert isinstance(d["expanded_nonstale"], int)
    assert isinstance(d["full_reset"], bool)
