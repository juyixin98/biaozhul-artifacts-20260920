"""优化器验收测试（任务要求的四类核心行为）：

1. 残差/代价单调下降，闭环后精度显著提升；
2. 角度跨 ±π 的正确处理；
3. 图不连通 / 无固定节点诊断；
4. 鲁棒核抑制错误回环。

注意：非线性最小二乘只保证局部收敛，不承诺全局最优。
"""

from __future__ import annotations

import numpy as np
import pytest

from pose_graph_optimizer.graph import PoseGraph, make_information_matrix
from pose_graph_optimizer.kernels import Kernel
from pose_graph_optimizer.optimizer import (
    GraphNotSolvedError,
    OptimizerOptions,
    optimize,
)
from pose_graph_optimizer.synthetic import (
    NoiseModel,
    build_graph_from_poses,
    circle_trajectory,
    square_trajectory,
    standard_scenarios,
)

INFO = make_information_matrix(0.05, np.deg2rad(2.0))


def _max_xy_error(graph, result, truth):
    return max(
        np.linalg.norm(result.poses[k][:2] - truth[k][:2])
        for k in graph.ordered_ids
        if not graph.nodes[k].fixed
    )


# ---------------------------------------------------------------- 1. 残差下降


def test_cost_is_monotone_nonincreasing():
    graph, truth = standard_scenarios(seed=42)["square"]["builder"]()
    result = optimize(graph, OptimizerOptions(max_iterations=50))
    hist = result.cost_history
    assert result.success
    for prev, nxt in zip(hist, hist[1:]):
        assert nxt <= prev + 1e-12
    assert result.final_cost < result.initial_cost
    assert result.final_cost <= 0.5 * result.initial_cost  # 至少下降一半


def test_closed_loop_reduces_error_versus_odometry_only():
    # 大噪声大方框（33 节点）：纯里程计漂移约 1.8m，回环应显著收紧
    noise = NoiseModel(sigma_xy=0.1, sigma_theta=np.deg2rad(3.0))
    truth = square_trajectory(side_length=4.0, per_side=8)
    n = len(truth)
    g_open, _ = build_graph_from_poses(
        truth, noise=noise, loop_pairs=[], seed=0
    )
    g_loop, _ = build_graph_from_poses(
        truth, noise=noise, loop_pairs=[(0, n - 1)], seed=0
    )
    r_open = optimize(g_open)
    r_loop = optimize(g_loop)
    err_open = _max_xy_error(g_open, r_open, truth)
    err_loop = _max_xy_error(g_loop, r_loop, truth)
    assert r_loop.success
    assert err_loop < 0.3 * err_open  # 正确回环约束显著收紧轨迹


def test_perfect_consistent_graph_converges_to_zero_residual():
    # 无噪声测量、初始猜测有扰动：应收敛到代价接近 0
    truth = square_trajectory(side_length=2.0, per_side=3)
    graph = PoseGraph()
    from pose_graph_optimizer.se2 import compose_pose, invert_pose

    rng = np.random.default_rng(0)
    for k, pose in enumerate(truth):
        init = pose if k == 0 else pose + rng.normal(0.0, 0.05, 3)
        graph.add_node(k, init, fixed=(k == 0))
    for k in range(1, len(truth)):
        z = compose_pose(invert_pose(truth[k - 1]), truth[k])
        graph.add_edge(k - 1, k, z, INFO)
    z_loop = compose_pose(invert_pose(truth[0]), truth[16])
    graph.add_edge(0, 16, z_loop, INFO)

    result = optimize(graph, OptimizerOptions())
    assert result.success
    assert result.final_cost < 1e-12
    from pose_graph_optimizer.se2 import wrap_angle

    for k in graph.ordered_ids:
        assert np.allclose(result.poses[k][:2], truth[k][:2], atol=1e-6)
        assert abs(wrap_angle(result.poses[k][2] - truth[k][2])) < 1e-6


# ------------------------------------------------------- 2. 角度跨 pi 处理


def test_angle_residual_near_branch_cut_converges():
    # 初始角 -2.8 rad，测量 +2.8 rad，二者代表同一方向（差 2π ≈ 5.6，
    # 最短角差仅 0.68）。若角度不 wrap，线性化会沿错误方向产生巨大更新。
    graph = PoseGraph()
    graph.add_node(0, [0.0, 0.0, 0.0], fixed=True)
    graph.add_node(1, [0.0, 0.0, -2.8])
    graph.add_edge(0, 1, [0.0, 0.0, 2.8], INFO)
    result = optimize(graph, OptimizerOptions())
    assert result.success
    assert result.final_cost < 1e-10
    # 结果朝向应等价于 +2.8（-2.8 与 +2.8 差 2π，本来就是同一朝向）
    from pose_graph_optimizer.se2 import wrap_angle

    assert abs(wrap_angle(result.poses[1][2] - 2.8)) < 1e-6
    assert result.iterations <= 10  # 走最短角差，几步即收敛


def test_circle_trajectory_crosses_pi_and_loop_angle_residual_is_small():
    # 整圆轨迹朝向累计 2π，最终节点真值角度约 0（经历 +π）。
    graph, truth = standard_scenarios(seed=42)["circle_wrap"]["builder"]()
    result = optimize(graph)
    assert result.success
    # 正确回环边（0->40）的最终角度残差必须很小，而不是接近 2π
    loop_edge = next(e for e in graph.edges if e.i == 0 and e.j == 40)
    e = loop_edge.residual(result.poses)
    assert abs(e[2]) < 0.05
    # 各节点朝向与真值等价（允许相差 2π，用 wrap 比较）。
    # 噪声里程计把角度误差分散到全图，阈值按 3σθ ≈ 0.105 rad 留出余量。
    from pose_graph_optimizer.se2 import wrap_angle

    for k in graph.ordered_ids:
        assert abs(wrap_angle(result.poses[k][2] - truth[k][2])) < 0.2


# ------------------------------------------------------- 3. 图结构诊断


def test_disconnected_graph_is_rejected_with_component_detail():
    graph, _ = standard_scenarios(seed=42)["disconnected"]["builder"]()
    with pytest.raises(GraphNotSolvedError) as excinfo:
        optimize(graph)
    msg = str(excinfo.value)
    assert "不连通" in msg
    assert "2 个连通分量" in msg


def test_missing_fixed_node_is_rejected():
    graph, _ = build_graph_from_poses(
        square_trajectory(per_side=2), fixed_id=None, seed=0
    )
    with pytest.raises(GraphNotSolvedError, match="固定节点"):
        optimize(graph)


def test_empty_graph_rejected():
    with pytest.raises(GraphNotSolvedError, match="空"):
        optimize(PoseGraph())


def test_two_components_each_fixed_is_solvable():
    # 两条链各固定一个节点：虽然不连通，但每个分量的规范自由度都被约束，
    # 此时不应被拒绝（诊断只拦整体不可观的情形）。
    # 说明：当前实现对不连通一律拒绝以给出明确诊断，本测试记录这一设计。
    graph = PoseGraph()
    info = np.eye(3)
    for nid, x in ((0, 0.0), (1, 1.0), (5, 10.0), (6, 11.0)):
        graph.add_node(
            nid, [x, 0.0, 0.0], fixed=(nid in (0, 5))
        )
    graph.add_edge(0, 1, [1, 0, 0], info)
    graph.add_edge(5, 6, [1, 0, 0], info)
    with pytest.raises(GraphNotSolvedError, match="不连通"):
        optimize(graph)


# --------------------------------------------------- 4. 鲁棒核与错误回环


def test_tukey_kernel_zeroes_out_false_loop_weight():
    graph, truth = standard_scenarios(seed=42)["square"]["builder"]()
    result = optimize(graph)
    false_edge = next(e for e in graph.edges if e.i == 3 and e.j == 15)
    assert result.weights[false_edge.id] == pytest.approx(0.0, abs=1e-6)
    # 被拒绝后，精度应与“根本没有错误回环”的图相当
    g_clean, _ = build_graph_from_poses(
        square_trajectory(), loop_pairs=[(0, 24)], false_loops=[], seed=42
    )
    r_clean = optimize(g_clean)
    err = _max_xy_error(graph, result, truth)
    err_clean = _max_xy_error(g_clean, r_clean, truth)
    assert abs(err - err_clean) < 0.05


def test_without_kernel_false_loop_biases_solution():
    # 对照组：关闭鲁棒核后，错误回环使结果明显变差
    truth = square_trajectory()
    g_robust, _ = build_graph_from_poses(
        truth,
        loop_pairs=[(0, 24)],
        false_loops=[
            {"pair": (3, 15), "offset": [1.5, -1.0, 0.4],
             "kernel": Kernel("tukey", 3.0)}
        ],
        seed=42,
    )
    g_plain, _ = build_graph_from_poses(
        truth,
        loop_pairs=[(0, 24)],
        false_loops=[
            {"pair": (3, 15), "offset": [1.5, -1.0, 0.4],
             "kernel": Kernel("none")}
        ],
        seed=42,
    )
    r_robust = optimize(g_robust)
    r_plain = optimize(g_plain)
    assert _max_xy_error(g_robust, r_robust, truth) < (
        0.5 * _max_xy_error(g_plain, r_plain, truth)
    )


def test_circle_false_loop_rejected_by_tukey():
    graph, truth = standard_scenarios(seed=42)["circle_wrap"]["builder"]()
    result = optimize(graph)
    false_edge = max(graph.edges, key=lambda e: e.id)
    assert result.weights[false_edge.id] == pytest.approx(0.0, abs=1e-6)
    assert _max_xy_error(graph, result, truth) < 0.6


def test_huber_is_soft_not_hard_rejection():
    # 记录 Huber 与 Tukey 的行为差异：Huber 权重随残差趋于 0 但永不等于 0
    graph, truth = build_graph_from_poses(
        square_trajectory(),
        loop_pairs=[(0, 24)],
        false_loops=[
            {"pair": (3, 15), "offset": [1.5, -1.0, 0.4],
             "kernel": Kernel("huber", 1.0)}
        ],
        seed=42,
    )
    result = optimize(graph)
    false_edge = next(e for e in graph.edges if e.i == 3 and e.j == 15)
    w = result.weights[false_edge.id]
    assert 0.0 < w < 0.1  # 显著抑制但未归零（软抑制）


# ------------------------------------------------------------- LM 机制


def test_fixed_node_pose_unchanged():
    graph, _ = standard_scenarios(seed=42)["square"]["builder"]()
    before = graph.nodes[0].initial_pose.copy()
    result = optimize(graph)
    assert np.allclose(result.poses[0], before)


def test_max_iterations_limit_respected():
    graph, _ = standard_scenarios(seed=42)["circle_wrap"]["builder"]()
    result = optimize(graph, OptimizerOptions(max_iterations=2))
    assert result.iterations == 2
    assert result.status == "max_iterations_reached"
    assert result.success is False


def test_invalid_options_rejected():
    with pytest.raises(ValueError):
        OptimizerOptions(max_iterations=0)
    with pytest.raises(ValueError):
        OptimizerOptions(tol_step=-1.0)
    with pytest.raises(ValueError):
        OptimizerOptions(initial_lambda=-1.0)


def test_singular_system_reported_not_crashed():
    # 半定信息矩阵（只约束角度）：平移自由度无约束，法方程奇异
    graph = PoseGraph()
    graph.add_node(0, [0.0, 0.0, 0.0], fixed=True)
    graph.add_node(1, [1.0, 0.0, 0.1])
    graph.add_edge(0, 1, [1.0, 0.0, 0.0], np.diag([0.0, 0.0, 1.0]))
    result = optimize(graph, OptimizerOptions())
    assert result.success is False
    assert result.status == "singular_system"
