"""Integration/acceptance tests for the nonlinear least-squares solver."""

import numpy as np
import pytest

from pose_graph.graph import GraphStructureError, PoseGraph
from pose_graph.optimizer import OptimizeOptions, optimize
from pose_graph.se2 import wrap_angle
from pose_graph.synthetic import (
    build_angle_cut_graph,
    build_long_turn_chain,
    build_square_graph,
    build_two_component_graph,
)


def test_square_loop_residual_decreases_and_recovers_ground_truth():
    graph, gt = build_square_graph()
    result = optimize(graph, OptimizeOptions())

    assert result.converged is True
    assert result.cost_final < result.cost_initial * 0.01
    # Recorded history must be monotonically non-increasing (LM rejects bad steps).
    assert all(
        later <= earlier + 1e-12
        for earlier, later in zip(result.cost_history, result.cost_history[1:])
    )
    max_xy = max(
        np.linalg.norm(result.poses[k][:2] - gt[k][:2]) for k in result.poses
    )
    max_theta = max(
        abs(wrap_angle(result.poses[k][2] - gt[k][2])) for k in result.poses
    )
    assert max_xy < 0.5
    assert max_theta < 0.1
    # The fixed node is the gauge anchor and never moves.
    np.testing.assert_allclose(result.poses[0], gt[0], atol=1e-12)


def test_fixed_node_is_required_or_auto_anchored():
    graph, _ = build_square_graph(fix_first=False)
    # No fixed node at all: the single component gets auto-anchored.
    result = optimize(graph, OptimizeOptions())
    assert result.auto_anchored_nodes == [0]
    assert result.cost_final < result.cost_initial
    # Strict mode refuses instead.
    graph2, _ = build_square_graph(fix_first=False)
    with pytest.raises(GraphStructureError):
        optimize(graph2, OptimizeOptions(strict_anchoring=True))


def test_bad_loop_closure_without_robust_kernel_distorts_trajectory():
    graph, gt = build_square_graph(bad_loop_closure=True, bad_kernel_type="linear")
    result = optimize(graph, OptimizeOptions())
    max_xy = max(
        np.linalg.norm(result.poses[k][:2] - gt[k][:2]) for k in result.poses
    )
    assert max_xy > 2.0  # plain LS bends the trajectory to fit the false edge


@pytest.mark.parametrize("kernel", ["cauchy", "geman_mcclure"])
def test_robust_kernel_rejects_bad_loop_closure(kernel):
    graph, gt = build_square_graph(
        bad_loop_closure=True,
        bad_kernel_type=kernel,
        bad_kernel_parameter=1.0,
    )
    result = optimize(graph, OptimizeOptions())
    max_xy = max(
        np.linalg.norm(result.poses[k][:2] - gt[k][:2]) for k in result.poses
    )
    bad_stat = next(s for s in result.edge_stats if s.label == "bad_loop_closure")
    # A redescending kernel stays near ground truth and leaves the outlier
    # residual large (the edge is effectively switched off).
    assert max_xy < 1.2
    assert bad_stat.squared_residual_final > 10.0


def test_huber_only_clips_does_not_fully_reject_extreme_outlier():
    """Huber is NOT redescending: weight tends to k/sqrt(s) > 0, never to 0.

    A grossly wrong loop closure therefore still biases the solution, just
    less than plain least squares.  This test documents that trade-off.
    """
    errors = {}
    for kernel in ["linear", "huber"]:
        graph, gt = build_square_graph(
            bad_loop_closure=True,
            bad_kernel_type=kernel,
            bad_kernel_parameter=1.0,
        )
        result = optimize(graph, OptimizeOptions())
        errors[kernel] = max(
            np.linalg.norm(result.poses[k][:2] - gt[k][:2]) for k in result.poses
        )
    assert errors["huber"] < errors["linear"]  # clipped, less distortion
    assert errors["huber"] > 1.5  # ...but still substantially biased


def test_robust_kernel_outperforms_linear_on_contaminated_graph():
    errors = {}
    for kernel in ["linear", "cauchy", "geman_mcclure"]:
        graph, gt = build_square_graph(
            bad_loop_closure=True,
            bad_kernel_type=kernel,
            bad_kernel_parameter=1.0,
        )
        result = optimize(graph, OptimizeOptions())
        errors[kernel] = max(
            np.linalg.norm(result.poses[k][:2] - gt[k][:2]) for k in result.poses
        )
    assert errors["geman_mcclure"] < errors["linear"] / 5
    assert errors["cauchy"] < errors["linear"]


def test_angle_across_pi_cut_converges_short_way():
    graph = build_angle_cut_graph()
    result = optimize(graph, OptimizeOptions())
    assert result.cost_final < 1e-12
    assert result.max_angular_residual_final < 1e-6
    # Initial raw angular error near the cut is small only thanks to wrapping.
    assert result.max_angular_residual_initial < 0.5
    assert abs(wrap_angle(result.poses[1][2] - 3.05)) < 1e-6


def test_long_turn_chain_accumulating_beyond_two_pi():
    graph = build_long_turn_chain()
    result = optimize(graph, OptimizeOptions())
    # Per-edge angular residuals must all be tiny despite >2pi total heading.
    assert result.max_angular_residual_final < 1e-6
    assert result.cost_final < 1e-12


def test_disconnected_graph_is_diagnosed_and_optimized_componentwise():
    graph = build_two_component_graph()
    report = graph.diagnose()
    assert report["is_connected"] is False
    assert report["num_components"] == 2
    result = optimize(graph, OptimizeOptions())
    # First component is user-fixed; second is automatically anchored.
    assert result.fixed_nodes == [0]
    assert result.auto_anchored_nodes == [16]
    assert result.cost_final < result.cost_initial * 0.01
    # Connectivity report is echoed in the result.
    assert result.connectivity["num_components"] == 2


def test_strict_anchoring_reports_unanchored_component():
    graph = build_two_component_graph()
    with pytest.raises(GraphStructureError, match="without a fixed node"):
        optimize(graph, OptimizeOptions(strict_anchoring=True))


def test_empty_graph_rejected():
    with pytest.raises(GraphStructureError):
        optimize(PoseGraph(), OptimizeOptions())


def test_cost_history_starts_at_initial_cost():
    graph, _ = build_square_graph()
    result = optimize(graph, OptimizeOptions())
    assert result.cost_history[0] == pytest.approx(result.cost_initial)
    assert result.cost_history[-1] == pytest.approx(result.cost_final)


def test_solver_is_local_no_global_guarantee_in_termination():
    # Sanity: severely wrong initialization (half-turn flip) still terminates;
    # we only assert finite output, deliberately not global optimality.
    graph, _ = build_square_graph()
    for node in graph.nodes:
        if node.index != 0:
            node.initial_pose[2] += np.pi  # flip every initial heading
    result = optimize(graph, OptimizeOptions(max_iterations=200))
    assert np.isfinite(result.cost_final)
    assert result.termination_reason
