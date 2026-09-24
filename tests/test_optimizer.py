"""Optimizer behaviour on synthetic graphs."""

import numpy as np
import pytest

from pgo import Edge, OptimizeOptions, PoseGraph, optimize
from pgo.synthetic import build_synthetic_graph, trajectory_rmse


def test_cost_decreases_and_converges():
    data = build_synthetic_graph(add_outlier=False, seed=42)
    res = optimize(data.graph, OptimizeOptions(kernel="none", max_iterations=50))
    assert res.final_cost < res.initial_cost
    assert res.final_cost < 1.0
    assert res.converged in ("cost", "step", "gradient")
    assert res.final_gradient_inf_norm < 1e-3
    assert not res.degenerate
    assert res.connected_components == 1
    assert res.min_eigenvalue is not None and res.min_eigenvalue > 0


def test_recovers_ground_truth_without_outlier():
    data = build_synthetic_graph(add_outlier=False, seed=42)
    res = optimize(data.graph, OptimizeOptions(kernel="none", max_iterations=50))
    assert trajectory_rmse(res.poses, data.ground_truth) < 0.15


def test_first_node_fixed_by_default():
    data = build_synthetic_graph(add_outlier=False, seed=42)
    res = optimize(data.graph, OptimizeOptions(max_iterations=50))
    # node 0 must not move: it anchors the gauge
    assert res.poses[0] == pytest.approx(data.graph.poses[0], abs=1e-12)


def test_robust_kernel_rejects_wrong_loop_closure():
    data = build_synthetic_graph(add_outlier=True, seed=42)
    ls = optimize(data.graph, OptimizeOptions(kernel="none", max_iterations=50))
    huber = optimize(
        data.graph, OptimizeOptions(kernel="huber", kernel_delta=1.0, max_iterations=50)
    )
    cauchy = optimize(
        data.graph, OptimizeOptions(kernel="cauchy", kernel_delta=1.0, max_iterations=50)
    )
    rmse_ls = trajectory_rmse(ls.poses, data.ground_truth)
    rmse_huber = trajectory_rmse(huber.poses, data.ground_truth)
    rmse_cauchy = trajectory_rmse(cauchy.poses, data.ground_truth)
    assert rmse_huber < 0.6 * rmse_ls
    assert rmse_cauchy < 0.3 * rmse_ls
    # the outlier edge keeps a large robust cost while normal edges stay small
    oi = data.outlier_edge_index
    assert huber.edge_costs[oi] > 5.0 * np.median(np.delete(huber.edge_costs, oi))


def test_degenerate_when_graph_disconnected():
    # two disconnected chains; fixing only node 0 leaves the second chain free
    poses = np.array(
        [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [5.0, 5.0, 0.0], [6.0, 5.0, 0.0]]
    )
    edges = [
        Edge(0, 1, np.array([1.0, 0.0, 0.0]), np.eye(3)),
        Edge(2, 3, np.array([1.0, 0.0, 0.0]), np.eye(3)),
    ]
    res = optimize(PoseGraph(poses, edges), OptimizeOptions(max_iterations=10))
    assert res.connected_components == 2
    assert res.degenerate


def test_perfect_graph_converges_to_zero_cost():
    poses = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [1.0, 1.0, np.pi / 2]])
    edges = [
        Edge(0, 1, np.array([1.0, 0.0, 0.0]), np.eye(3)),
        Edge(1, 2, np.array([0.0, 1.0, np.pi / 2]), np.eye(3)),
        Edge(0, 2, np.array([1.0, 1.0, np.pi / 2]), np.eye(3)),
    ]
    res = optimize(PoseGraph(poses, edges), OptimizeOptions(max_iterations=10))
    assert res.final_cost < 1e-12
    assert res.final_gradient_inf_norm < 1e-6


def test_invalid_information_matrix_rejected():
    with pytest.raises(ValueError):
        Edge(0, 1, np.zeros(3), np.zeros((3, 3)))  # not positive definite
    with pytest.raises(ValueError):
        Edge(0, 1, np.zeros(3), np.array([[1.0, 2.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]))
