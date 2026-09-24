"""Synthetic SE(2) pose-graph datasets with (correct and wrong) loop closures.

The robot drives a square trajectory.  Odometry edges connect consecutive
poses; loop-closure edges connect poses that are close in the ground-truth
trajectory.  Optional *wrong* loop closures simulate false data association:
the measurement is consistent with a different pose pair than the one the
edge connects.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .optimizer import Edge
from .se2 import between, compose


@dataclass
class SyntheticDataset:
    true_poses: np.ndarray  # (N, 3) ground truth
    initial_poses: np.ndarray  # (N, 3) noisy dead-reckoning guess
    edges: list[Edge]  # odometry + correct loop closures
    wrong_loop_edges: list[Edge]  # false loop closures (kept separate)


def _information(std: tuple[float, float, float]) -> np.ndarray:
    return np.diag([1.0 / std[0] ** 2, 1.0 / std[1] ** 2, 1.0 / std[2] ** 2])


def generate_square_dataset(
    side_length: float = 10.0,
    steps_per_side: int = 10,
    odom_std: tuple[float, float, float] = (0.05, 0.05, 0.02),
    loop_std: tuple[float, float, float] = (0.1, 0.1, 0.03),
    loop_distance: float = 2.5,
    min_loop_separation: int = 10,
    n_wrong_loops: int = 0,
    seed: int = 0,
) -> SyntheticDataset:
    """Generate a square-trajectory pose graph.

    Parameters
    ----------
    side_length, steps_per_side:
        Geometry of the square trajectory.
    odom_std, loop_std:
        Measurement noise standard deviations (x, y, theta).
    loop_distance:
        Maximum ground-truth distance for a correct loop closure.
    min_loop_separation:
        Minimum index gap between loop-closure endpoints.
    n_wrong_loops:
        Number of false loop-closure edges to generate.
    seed:
        RNG seed for reproducibility.
    """
    rng = np.random.default_rng(seed)

    # --- Ground-truth square trajectory ---------------------------------
    step = side_length / steps_per_side
    headings = [0.0, np.pi / 2, np.pi, -np.pi / 2]
    true_poses = [np.zeros(3)]
    for side in range(4):
        heading = headings[side]
        for k in range(steps_per_side):
            motion = np.array([step, 0.0, 0.0])
            if k == 0 and side > 0:
                # Turn to the new heading at the corner.
                turn = headings[side] - headings[side - 1]
                motion = np.array([0.0, 0.0, turn])
                motion = compose(motion, np.array([step, 0.0, 0.0]))
            true_poses.append(compose(true_poses[-1], motion))
    true_poses = np.array(true_poses)
    n = len(true_poses)

    odom_info = _information(odom_std)
    loop_info = _information(loop_std)

    def noisy(rel: np.ndarray, std: tuple[float, float, float]) -> np.ndarray:
        return compose(rel, rng.normal(0.0, std))

    # --- Odometry edges + dead-reckoning initial guess -------------------
    edges: list[Edge] = []
    initial = [np.zeros(3)]
    for i in range(n - 1):
        z = noisy(between(true_poses[i], true_poses[i + 1]), odom_std)
        edges.append(Edge(i=i, j=i + 1, z=z, omega=odom_info))
        initial.append(compose(initial[-1], z))
    initial_poses = np.array(initial)

    # --- Correct loop closures -------------------------------------------
    for i in range(n):
        for j in range(i + min_loop_separation, n):
            dist = np.linalg.norm(true_poses[i][:2] - true_poses[j][:2])
            if dist < loop_distance:
                z = noisy(between(true_poses[i], true_poses[j]), loop_std)
                edges.append(Edge(i=i, j=j, z=z, omega=loop_info))

    # --- Wrong loop closures (false data association) ---------------------
    wrong_edges: list[Edge] = []
    if n_wrong_loops > 0:
        candidates = [
            (i, j)
            for i in range(n)
            for j in range(i + min_loop_separation, n)
            if np.linalg.norm(true_poses[i][:2] - true_poses[j][:2])
            > 2.0 * loop_distance
        ]
        picks = rng.choice(len(candidates), size=n_wrong_loops, replace=False)
        for idx in picks:
            i, j = candidates[int(idx)]
            # Measurement that would be correct for a *different* pair (i, k).
            k = int(rng.integers(0, n))
            while k == i:
                k = int(rng.integers(0, n))
            z = noisy(between(true_poses[i], true_poses[k]), loop_std)
            wrong_edges.append(Edge(i=i, j=j, z=z, omega=loop_info))

    return SyntheticDataset(
        true_poses=true_poses,
        initial_poses=initial_poses,
        edges=edges,
        wrong_loop_edges=wrong_edges,
    )
