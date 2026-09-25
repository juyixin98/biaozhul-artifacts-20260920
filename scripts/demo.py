"""End-to-end demonstration on synthetic data with outliers.

Run:  python scripts/demo.py

Prints a side-by-side comparison of OLS and Huber (IRLS) on the same data,
plus edge cases (rank deficiency, exact zero residuals), and also exercises
the JSON request handler. The RNG seed is fixed so the report is
reproducible.
"""

import json
import os
import sys

import numpy as np

# Allow running directly as `python scripts/demo.py` from the repo root
# without installing the package.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from robustreg import handle_request, huber_irls, ols_fit  # noqa: E402
from robustreg.wls import RankDeficientError  # noqa: E402

SEED = 20260923


def section(title):
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def main():
    rng = np.random.default_rng(SEED)

    # ------------------------------------------------------------------
    section("1. Synthetic data: y = b0 + X b + noise, then gross outliers")
    n, p = 120, 4
    X = rng.normal(size=(n, p))
    true_b = np.array([2.5, -1.0, 0.5, 3.0])
    true_b0 = -0.75
    y = true_b0 + X @ true_b + rng.normal(scale=0.4, size=n)
    n_out = n // 7
    out_idx = rng.choice(n, size=n_out, replace=False)
    signs = rng.choice([-1.0, 1.0], size=n_out)
    y[out_idx] += signs * rng.uniform(10, 20, size=n_out)
    print(f"n={n}, p={p}, outliers={n_out} (indices {sorted(out_idx.tolist())})")
    print(f"true intercept={true_b0}, true slopes={true_b.tolist()}")

    ols = ols_fit(X, y)
    hub = huber_irls(X, y, delta=1.345, tol=1e-10)

    def errrow(name, coef, intercept):
        e = np.r_[coef, intercept] - np.r_[true_b, true_b0]
        return (f"{name:6s} intercept={intercept:8.4f}  slopes="
                f"{np.round(coef, 4).tolist()}  max|err|={np.max(np.abs(e)):.4f}")

    print(errrow("OLS", ols.coef, ols.intercept))
    print(errrow("Huber", hub.coef, hub.intercept))
    print(f"\nHuber objective at OLS solution : {ols.huber_objective:.6f}")
    print(f"Huber objective at Huber solution: {hub.objective:.6f}")
    print(f"converged={hub.converged} after {hub.iterations} IRLS iterations; "
          f"objective monotone={hub.objective_decreased}")
    n_inlier = int(np.sum(hub.weights == 1.0))
    print(f"final weights: {n_inlier} inliers (w=1), "
          f"{n - n_inlier} down-weighted (w<1)")

    # ------------------------------------------------------------------
    section("2. Objective history (must descend)")
    for i, v in enumerate(hub.objective_history):
        print(f"  iter {i:2d}: {v:.8f}")

    # ------------------------------------------------------------------
    section("3. Rank-deficient design: exact collinear columns")
    Xc = np.column_stack([X[:, 0], X[:, 1], X[:, 0] + X[:, 1]])
    try:
        huber_irls(Xc, y)
        print("ERROR: deficiency not detected")
    except RankDeficientError as e:
        print(f"RankDeficientError: numerical rank {e.rank}/{e.p}, "
              f"nullspace dim={e.nullspace_dim}, smallest singular value="
              f"{e.smallest_singular_value:.3e}, threshold={e.threshold:.3e}")
    print("Recovery options: remove a column, set lam>0 (ridge on slopes), "
          "or allow_rank_deficient=true (minimum-norm).")
    ridge = huber_irls(Xc, y, lam=1e-3)
    print(f"With lam=1e-3: rank={ridge.rank}, converged={ridge.converged}, "
          f"objective={ridge.objective:.4f}")

    # ------------------------------------------------------------------
    section("4. Exactly zero residuals (perfect linear fit)")
    Xz = np.array([[0.0, 0.0], [1.0, 0.0], [0.0, 1.0],
                   [1.0, 1.0], [2.0, 1.0]])
    yz = 1.0 + 2.0 * Xz[:, 0] - 3.0 * Xz[:, 1]
    z = huber_irls(Xz, yz)
    print(f"coef={z.coef.tolist()}, intercept={z.intercept:.6f}, "
          f"iterations={z.iterations}")
    print(f"residuals={np.round(z.residual, 12).tolist()}")
    print(f"weights={z.weights.tolist()} (all finite, all 1)")
    print(f"objective={z.objective:.3e}, converged={z.converged}")

    # ------------------------------------------------------------------
    section("5. Reproducibility through the JSON interface")
    request = {"X": X.tolist(), "y": y.tolist(), "delta": 1.345,
               "tol": 1e-10, "max_iter": 100}
    r1 = handle_request(request)
    r2 = handle_request(request)
    print("two identical requests -> byte-identical result:", r1 == r2)
    print(json.dumps({
        "ok": r1["ok"],
        "method": r1["method"],
        "status": r1["result"]["status"],
        "iterations": r1["result"]["iterations"],
        "objective": r1["result"]["objective"],
        "coef": r1["result"]["coef"],
        "intercept": r1["result"]["intercept"],
        "rank": r1["result"]["rank"],
        "converged": r1["result"]["converged"],
    }, indent=2))

    section("6. Rank deficiency through the JSON interface")
    bad = handle_request({"X": Xc.tolist(), "y": y.tolist()})
    print(json.dumps(bad, indent=2)[:900])


if __name__ == "__main__":
    main()
