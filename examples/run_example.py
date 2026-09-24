"""End-to-end example: synthetic square trajectory with loop closures.

Generates a noisy pose graph (including *wrong* loop closures), optimizes it
with and without the robust kernel, and prints cost / gradient / degeneracy
reports plus trajectory errors against ground truth.

Run from the repository root:

    .venv/bin/python examples/run_example.py
"""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pose_graph.optimizer import OptimizeOptions, optimize
from pose_graph.synthetic import generate_square_dataset


def mean_position_error(poses: np.ndarray, truth: np.ndarray) -> float:
    return float(np.mean(np.linalg.norm(poses[:, :2] - truth[:, :2], axis=1)))


def report(name: str, result, truth) -> None:
    print(f"--- {name} ---")
    print(f"  converged          : {result.converged} ({result.message})")
    print(f"  iterations         : {result.iterations}")
    print(f"  cost history       : "
          + " -> ".join(f"{c:.4f}" for c in result.cost_history))
    print(f"  final cost         : {result.final_cost:.6f}")
    print(f"  final |grad|_inf   : {result.final_gradient_norm:.3e}")
    deg = result.degeneracy
    print(f"  degenerate         : {deg['is_degenerate']}")
    print(f"  min eigenvalue     : {deg['min_eigenvalue']:.3e}")
    print(f"  condition estimate : {deg['condition_estimate']:.3e}")
    print(f"  mean position error: {mean_position_error(result.poses, truth):.4f} m")
    print()


def main() -> None:
    data = generate_square_dataset(seed=2, n_wrong_loops=4)
    n_edges = len(data.edges)
    print(f"poses: {len(data.true_poses)}, "
          f"edges: {n_edges} (+{len(data.wrong_loop_edges)} wrong loop closures)")
    print(f"initial mean position error: "
          f"{mean_position_error(data.initial_poses, data.true_poses):.4f} m")
    print()

    # 1. Clean graph (odometry + correct loop closures), robust kernel.
    report(
        "clean graph, huber kernel",
        optimize(data.initial_poses, data.edges),
        data.true_poses,
    )

    # 2. Graph with wrong loop closures, plain least squares.
    edges = data.edges + data.wrong_loop_edges
    report(
        "with wrong loop closures, NO robust kernel",
        optimize(data.initial_poses, edges, OptimizeOptions(robust_kernel="none")),
        data.true_poses,
    )

    # 3. Same graph, Huber kernel: false constraints get down-weighted.
    # (IRLS reweighting converges slowly, so allow more iterations.)
    report(
        "with wrong loop closures, huber kernel",
        optimize(
            data.initial_poses,
            edges,
            OptimizeOptions(robust_kernel="huber", max_iterations=200),
        ),
        data.true_poses,
    )


if __name__ == "__main__":
    main()
