"""End-to-end validation on a synthetic trajectory.

1. Builds a rectangular ground-truth loop, noisy odometry, a correct loop
   closure and one deliberately WRONG loop closure.
2. Checks the analytic edge Jacobians against central finite differences.
3. Optimizes with plain least squares and with a Huber robust kernel;
   reports cost / gradient / degeneracy and trajectory RMSE vs ground truth.
4. Prints per-edge robust cost, which exposes the rejected outlier.

Run:
    .venv/bin/python examples/run_demo.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pgo import (  # noqa: E402
    Edge,
    OptimizeOptions,
    edge_residual_and_jacobians,
    numerical_jacobian,
    optimize,
)
from pgo.synthetic import build_synthetic_graph, trajectory_rmse  # noqa: E402


def check_jacobians(num_checks: int = 50, seed: int = 7) -> tuple[float, float]:
    """Verify analytic Jacobians vs central differences on random edges."""
    rng = np.random.default_rng(seed)
    max_xy_err = 0.0
    max_th_err = 0.0
    for _ in range(num_checks):
        poses = rng.uniform(-3.0, 3.0, size=(4, 3))
        poses[:, 2] = rng.uniform(-np.pi, np.pi, size=4)
        edge = Edge(
            i=int(rng.integers(0, 2)),
            j=int(rng.integers(2, 4)),
            measurement=np.array(
                [rng.uniform(-1, 1), rng.uniform(-1, 1), rng.uniform(-1, 1)]
            ),
            information=np.diag([10.0, 10.0, 50.0]),
        )
        _, ai, aj = edge_residual_and_jacobians(poses, edge)

        def residual_of(vals):
            p = poses.copy()
            p[edge.i] = vals[:3]
            p[edge.j] = vals[3:]
            r, _, _ = edge_residual_and_jacobians(p, edge)
            return r

        stack = np.concatenate([poses[edge.i], poses[edge.j]])
        num = numerical_jacobian(residual_of, stack, eps=1e-7)
        num_i, num_j = num[:, :3], num[:, 3:]
        # avoid angle wrap discontinuity: errors here are well inside (-pi, pi)
        max_xy_err = max(
            max_xy_err,
            np.max(np.abs(ai[:2] - num_i[:2])),
            np.max(np.abs(aj[:2] - num_j[:2])),
        )
        max_th_err = max(
            max_th_err,
            abs(ai[2, 2] - num_i[2, 2]),
            abs(aj[2, 2] - num_j[2, 2]),
        )
    return max_xy_err, max_th_err


def run_case(data, kernel: str, delta: float, label: str) -> dict:
    opts = OptimizeOptions(kernel=kernel, kernel_delta=delta, max_iterations=50)
    res = optimize(data.graph, opts)
    rmse = trajectory_rmse(res.poses, data.ground_truth)
    print(f"\n=== {label} ===")
    print(f"  iterations            : {res.iterations} ({res.converged}; {res.message})")
    print(f"  initial cost          : {res.initial_cost:.6f}")
    print(f"  final cost            : {res.final_cost:.6f}")
    print(f"  initial grad inf-norm : {res.initial_gradient_inf_norm:.6e}")
    print(f"  final grad inf-norm   : {res.final_gradient_inf_norm:.6e}")
    print(f"  connected components  : {res.connected_components}")
    print(
        f"  normal-matrix spectrum: min={res.min_eigenvalue:.3e} max={res.max_eigenvalue:.3e}"
    )
    print(f"  degenerate            : {res.degenerate}")
    print(f"  trajectory RMSE (x,y) : {rmse:.4f} m")
    if data.outlier_edge_index is not None:
        oi = data.outlier_edge_index
        costs = res.edge_costs
        normal_med = float(np.median(np.delete(costs, oi)))
        print(
            f"  edge {oi} robust cost    : {costs[oi]:.4f} "
            f"(median normal edge: {normal_med:.4f})"
        )
    return {"result": res, "rmse": rmse}


def main() -> int:
    print("Synthetic trajectory: rectangle, 4x5 = 20 nodes")
    data = build_synthetic_graph(add_outlier=True, seed=42)
    print(
        f"  nodes={data.graph.num_nodes}, odometry edges={data.num_odometry}, "
        f"loop closures={data.num_loop_closures}, outlier=edge[{data.outlier_edge_index}]"
    )

    print("\n--- Jacobian check (analytic vs central differences) ---")
    max_xy_err, max_th_err = check_jacobians()
    print(f"  max abs error, translation blocks : {max_xy_err:.3e}")
    print(f"  max abs error, angular blocks     : {max_th_err:.3e}")
    jac_ok = max_xy_err < 1e-6 and max_th_err < 1e-6
    print(f"  PASS: {jac_ok}")

    ls = run_case(data, "none", 1.0, "Plain least squares (no robust kernel)")
    hub = run_case(data, "huber", 1.0, "Huber robust kernel (delta=1.0)")

    print("\n--- Conclusion ---")
    robust_wins = hub["rmse"] < 0.5 * ls["rmse"]
    print(f"  Huber reduces RMSE w.r.t. LS: {robust_wins} "
          f"({ls['rmse']:.4f} -> {hub['rmse']:.4f})")
    ok = bool(jac_ok and robust_wins and not hub["result"].degenerate)
    print(f"  overall validation: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
