"""Deterministic synthetic trajectories and sensor measurements.

Everything is generated in-process (no files, no hardware): a robot drives a
square loop, relative SE2 odometry and one loop-closure measurement are
derived from ground truth, and noise is added with a seeded RNG so results
are reproducible.
"""

import numpy as np

from .graph import PoseGraph
from .se2 import pose_compose, relative_pose, wrap_angle


def square_ground_truth(side=10.0, per_side=5):
    """Ground-truth poses around a square, 4*per_side nodes, node 0 at origin.

    Headings are the driving direction (0, pi/2, pi, 3pi/2).  The final pose
    is not duplicated; closure back to node 0 is an edge.
    """
    step = side / per_side
    poses = []
    for k in range(4 * per_side):
        seg, t = divmod(k, per_side)
        if seg == 0:
            poses.append(np.array([t * step, 0.0, 0.0]))
        elif seg == 1:
            poses.append(np.array([side, t * step, np.pi / 2]))
        elif seg == 2:
            poses.append(np.array([side - t * step, side, np.pi]))
        else:
            poses.append(np.array([0.0, side - t * step, 3 * np.pi / 2]))
    return poses


def _noisy_relative(rng, z, xy_sigma, theta_sigma):
    zn = z.copy()
    zn[0] += rng.normal(0.0, xy_sigma)
    zn[1] += rng.normal(0.0, xy_sigma)
    zn[2] = wrap_angle(zn[2] + rng.normal(0.0, theta_sigma))
    return zn


def build_square_graph(
    seed=7,
    side=10.0,
    per_side=5,
    xy_noise=0.05,
    theta_noise=0.01,
    info_translation=20.0,
    info_rotation=20.0,
    bad_loop_closure=False,
    bad_kernel_type="linear",
    bad_kernel_parameter=None,
    bad_loop_offset=(0.0, 3.0, 0.4),
    fix_first=True,
):
    """Build a square-loop pose graph with odometry drift and (optional) bad edge.

    The initial guess is dead-reckoning from the *noisy* odometry chain, so the
    trajectory visibly drifts until the loop closure pulls it closed.

    When ``bad_loop_closure`` is true an extra wrong loop edge is inserted with
    a 3 m lateral and 0.4 rad yaw offset, weighted like a real constraint;
    ``bad_kernel_type`` selects the robust kernel applied to that edge.
    """
    rng = np.random.default_rng(seed)
    gt = square_ground_truth(side, per_side)
    n = len(gt)
    information = np.diag(
        [info_translation, info_translation, info_rotation]
    )

    graph = PoseGraph()
    # Node 0 starts exactly at the known origin.
    graph.add_node(0, gt[0], fixed=fix_first)

    # Dead-reckoned initial guess, integrating noisy odometry.
    initial = [gt[0].copy()]
    odom_edges = []
    for k in range(n - 1):
        z_true = relative_pose(gt[k], gt[k + 1])
        z = _noisy_relative(rng, z_true, xy_noise, theta_noise)
        odom_edges.append((k, k + 1, z))
        initial.append(pose_compose(initial[-1], z))
        graph.add_node(k + 1, initial[-1])

    for i, j, z in odom_edges:
        graph.add_edge(i, j, z, information, label="odometry")

    # Correct loop closure: last node back to node 0.
    z_loop = relative_pose(gt[-1], gt[0])
    graph.add_edge(n - 1, 0, z_loop, information, label="loop_closure")

    if bad_loop_closure:
        i_bad, j_bad = per_side - 1, 3 * per_side + 1
        z_bad_true = relative_pose(gt[i_bad], gt[j_bad])
        ox, oy, oth = bad_loop_offset
        z_bad = z_bad_true + np.array([ox, oy, oth])
        graph.add_edge(
            i_bad,
            j_bad,
            z_bad,
            information,
            kernel_type=bad_kernel_type,
            kernel_parameter=bad_kernel_parameter,
            label="bad_loop_closure",
        )
    return graph, gt


def build_angle_cut_graph(z_theta=3.05, initial_theta_1=-3.0, initial_theta_2=-2.9):
    """Tiny graph whose true relative angle (~+pi) is across the +/-pi cut.

    The initial guess places the nodes at the equivalent negative angle, so the
    raw un-wrapped residual would be ~-6 rad (the long way around); with proper
    angle wrapping it is ~0.2 rad and the optimizer takes the short step.
    """
    graph = PoseGraph()
    information = np.diag([10.0, 10.0, 25.0])
    graph.add_node(0, [0.0, 0.0, 0.0], fixed=True)
    graph.add_node(1, [0.5, -0.2, initial_theta_1])
    graph.add_node(2, [0.8, -0.5, initial_theta_2])
    graph.add_edge(0, 1, [1.0, 0.0, z_theta], information, label="cut_edge_01")
    graph.add_edge(1, 2, [1.0, 0.0, 0.05], information, label="small_turn")
    return graph


def build_long_turn_chain(num_edges=15, dtheta=0.45):
    """Chain accumulating more than 2pi of heading; checks unwrapped tracking.

    Initial guesses dead-reckon slightly noisy odometry, so absolute headings
    pass the +/-pi cut multiple times; per-edge residual angles must stay small.
    """
    rng = np.random.default_rng(11)
    graph = PoseGraph()
    information = np.diag([20.0, 20.0, 25.0])
    graph.add_node(0, [0.0, 0.0, 0.0], fixed=True)
    initial = np.array([0.0, 0.0, 0.0])
    for k in range(num_edges):
        z = np.array([1.0, 0.0, dtheta])
        z_noisy = z + np.array(
            [rng.normal(0, 0.02), rng.normal(0, 0.02), rng.normal(0, 0.005)]
        )
        initial = pose_compose(initial, z_noisy)
        graph.add_node(k + 1, initial)
        graph.add_edge(k, k + 1, z, information, label="odometry")
    return graph


def build_two_component_graph():
    """Two disconnected square loops; only the first gets a fixed node."""
    g_a, _ = build_square_graph(seed=1, per_side=4, fix_first=True)
    g_b, gt_b = build_square_graph(seed=2, per_side=4, fix_first=False)
    offset = g_a.num_nodes()

    graph = PoseGraph()
    for node in g_a.nodes:
        graph.add_node(node.index, node.initial_pose, fixed=node.index in g_a.fixed_nodes)
    for edge in g_a.edges:
        graph.add_edge(
            edge.i, edge.j, edge.z, edge.information,
            kernel_type=edge.kernel_type, kernel_parameter=edge.kernel_parameter,
            label=edge.label,
        )
    for node in g_b.nodes:
        graph.add_node(node.index + offset, node.initial_pose, fixed=False)
    for edge in g_b.edges:
        graph.add_edge(
            edge.i + offset, edge.j + offset, edge.z, edge.information,
            kernel_type=edge.kernel_type, kernel_parameter=edge.kernel_parameter,
            label=edge.label + "_b",
        )
    return graph
