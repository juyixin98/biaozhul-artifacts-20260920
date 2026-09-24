"""Synthetic SE(2) trajectory generator used for validation and demos.

Builds a rectangular-loop ground-truth trajectory, noisy odometry edges
between consecutive poses, one correct loop-closure edge, and (optionally)
one *wrong* loop closure (a perceptual-aliasing outlier).  The optimizer
should recover the ground truth with a robust kernel while a plain
least-squares fit is visibly corrupted by the outlier.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .se2 import between, compose, wrap_angle
from .optimizer import Edge, PoseGraph


@dataclass
class SyntheticData:
    graph: PoseGraph
    ground_truth: np.ndarray  # (N, 3)
    num_odometry: int
    num_loop_closures: int
    outlier_edge_index: int | None  # index into graph.edges, or None


def _rectangle_ground_truth(side_steps: int, step: float) -> np.ndarray:
    """Closed rectangular loop: side_steps poses per side, 4*side_steps nodes."""
    poses = [np.array([0.0, 0.0, 0.0])]
    headings = [0.0, np.pi / 2, np.pi, -np.pi / 2]
    for side in range(4):
        heading = headings[side]
        for _ in range(side_steps):
            delta = np.array([step * np.cos(heading), step * np.sin(heading), 0.0])
            poses.append(compose(poses[-1], delta))
    # the last pose coincides with the first (loop closure node); drop it so
    # that the graph has 4*side_steps distinct nodes
    return np.array(poses[:-1])


def build_synthetic_graph(
    side_steps: int = 5,
    step: float = 1.0,
    odom_info: tuple[float, float, float] = (100.0, 100.0, 400.0),
    loop_info: tuple[float, float, float] = (50.0, 50.0, 200.0),
    noise_xy: float = 0.03,
    noise_theta: float = 0.01,
    drift_per_step: float = 0.02,
    add_outlier: bool = True,
    outlier_translation: float = 1.5,
    seed: int = 42,
) -> SyntheticData:
    """Create a noisy pose graph with a loop closure and an optional outlier.

    Node 0 of the returned graph sits exactly at the ground-truth origin so
    that the fixed gauge matches the ground-truth frame.
    """
    rng = np.random.default_rng(seed)
    gt = _rectangle_ground_truth(side_steps, step)
    n = gt.shape[0]

    odom_omega = np.diag(odom_info)
    loop_omega = np.diag(loop_info)

    # --- noisy odometry chain, integrated to form the initial guess --------
    odom_meas: list[np.ndarray] = []
    for k in range(n):
        true_rel = between(gt[k], gt[(k + 1) % n])
        noise = np.array(
            [
                rng.normal(0.0, noise_xy),
                rng.normal(0.0, noise_xy),
                rng.normal(0.0, noise_theta),
            ]
        )
        odom_meas.append(compose(true_rel, noise))

    init = [gt[0].copy()]
    for k in range(n - 1):
        # integrate odometry plus a small systematic drift so the initial
        # trajectory visibly bends away from the ground truth
        drift = np.array([drift_per_step, 0.0, drift_per_step * 0.15])
        init.append(compose(init[-1], compose(odom_meas[k], drift)))
    init = np.array(init)
    init[:, 2] = wrap_angle(init[:, 2])

    edges: list[Edge] = []
    for k in range(n - 1):
        edges.append(Edge(k, k + 1, odom_meas[k], odom_omega))
    num_odom = len(edges)

    # --- correct loop closure: last node back to node 0 --------------------
    true_loop = between(gt[n - 1], gt[0])
    noisy_loop = compose(
        true_loop,
        np.array(
            [
                rng.normal(0.0, noise_xy),
                rng.normal(0.0, noise_xy),
                rng.normal(0.0, noise_theta),
            ]
        ),
    )
    edges.append(Edge(n - 1, 0, noisy_loop, loop_omega))

    # --- wrong loop closure (perceptual aliasing outlier) ------------------
    outlier_idx = None
    if add_outlier:
        # node 1 falsely matched against the opposite side of the rectangle
        j = n // 2 + 1
        wrong = compose(
            between(gt[1], gt[j]),
            np.array([outlier_translation, -0.5 * outlier_translation, 0.6]),
        )
        outlier_idx = len(edges)
        edges.append(Edge(1, j, wrong, loop_omega))

    graph = PoseGraph(init, edges)
    return SyntheticData(
        graph=graph,
        ground_truth=gt,
        num_odometry=num_odom,
        num_loop_closures=len(edges) - num_odom,
        outlier_edge_index=outlier_idx,
    )


def trajectory_rmse(estimate: np.ndarray, ground_truth: np.ndarray) -> float:
    """RMSE of (x, y) positions after aligning the shared gauge at node 0."""
    est = np.asarray(estimate, dtype=float)
    gt = np.asarray(ground_truth, dtype=float)
    diff = est[:, :2] - gt[:, :2]
    return float(np.sqrt(np.mean(np.sum(diff * diff, axis=1))))
