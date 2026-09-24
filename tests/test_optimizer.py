"""End-to-end optimizer tests on synthetic trajectories.

Covers: convergence with loop closures, robustness against wrong loop
closures, gauge fixing, and degeneracy reporting.
"""

import numpy as np

from pose_graph.optimizer import Edge, OptimizeOptions, optimize
from pose_graph.synthetic import generate_square_dataset


def _mean_position_error(poses, truth):
    return float(np.mean(np.linalg.norm(poses[:, :2] - truth[:, :2], axis=1)))


def test_converges_with_loop_closures():
    data = generate_square_dataset(seed=1)
    result = optimize(data.initial_poses, data.edges)

    assert result.converged, result.message
    assert result.iterations >= 2
    # Cost must decrease substantially and monotonically (line search).
    costs = result.cost_history
    assert costs[-1] < 0.5 * costs[0]
    assert all(c2 <= c1 + 1e-9 for c1, c2 in zip(costs, costs[1:]))
    # Gradient must shrink.
    assert result.gradient_norm_history[-1] < 1e-2 * result.gradient_norm_history[0]
    # Optimized trajectory must be much closer to ground truth than the
    # dead-reckoning initial guess.
    err_before = _mean_position_error(data.initial_poses, data.true_poses)
    err_after = _mean_position_error(result.poses, data.true_poses)
    assert err_after < 0.5 * err_before
    assert err_after < 0.25
    # Gauge: node 0 stays fixed at its initial pose.
    assert np.allclose(result.poses[0], data.initial_poses[0], atol=1e-12)
    # Well-constrained graph must not be flagged degenerate.
    assert not result.degeneracy["is_degenerate"]
    assert result.degeneracy["min_eigenvalue"] > 0


def test_robust_kernel_rejects_wrong_loop_closures():
    data = generate_square_dataset(seed=2, n_wrong_loops=4)
    edges = data.edges + data.wrong_loop_edges

    plain = optimize(
        data.initial_poses, edges, OptimizeOptions(robust_kernel="none")
    )
    robust = optimize(
        data.initial_poses,
        edges,
        OptimizeOptions(robust_kernel="huber", max_iterations=200),
    )

    err_plain = _mean_position_error(plain.poses, data.true_poses)
    err_robust = _mean_position_error(robust.poses, data.true_poses)

    # The Huber kernel must clearly outperform the plain least squares.
    assert robust.converged
    assert err_robust < err_plain
    assert err_robust < 0.5
    # With false constraints, plain least squares should be visibly distorted.
    assert err_plain > 0.5


def test_degeneracy_detected_for_disconnected_graph():
    # Two disconnected chains: the second component has a free gauge, so the
    # anchored Hessian is singular.
    info = np.eye(3)
    edges = [
        Edge(0, 1, np.array([1.0, 0.0, 0.0]), info),
        Edge(1, 2, np.array([1.0, 0.0, 0.0]), info),
        Edge(3, 4, np.array([1.0, 0.0, 0.0]), info),
        Edge(4, 5, np.array([1.0, 0.0, 0.0]), info),
    ]
    initial = np.zeros((6, 3))
    result = optimize(initial, edges)
    assert result.degeneracy["is_degenerate"]
    assert result.degeneracy["min_eigenvalue"] < 1e-9


def test_fix_node_option():
    data = generate_square_dataset(seed=3)
    fix = 5
    result = optimize(
        data.initial_poses, data.edges, OptimizeOptions(fix_node=fix)
    )
    assert np.allclose(result.poses[fix], data.initial_poses[fix], atol=1e-12)


def test_invalid_options_raise():
    data = generate_square_dataset(seed=4)
    try:
        optimize(data.initial_poses, data.edges, OptimizeOptions(fix_node=999))
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for out-of-range fix_node")
    try:
        optimize(
            data.initial_poses, data.edges, OptimizeOptions(robust_kernel="cauchy")
        )
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for unknown kernel")
