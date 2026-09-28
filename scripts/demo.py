"""End-to-end demonstration: low-precision factorize + high-precision residual.

Runs three local synthetic scenarios and prints, for each, the direct float32
and float64 answers versus the iteratively refined answer, the independently
recomputed backward error, the estimated condition number and the service's
accept/reject decision.

    python scripts/demo.py

Everything is synthetic and local; no network or external accounts are used.
"""

from __future__ import annotations

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import Config  # noqa: E402
from app.numerical import mp, mpf, workprec  # noqa: E402
from app.numerical.engine import solve_system  # noqa: E402
from tests.fixtures import (  # noqa: E402
    conditioned_matrix,
    direct_low_precision_solve,
    exact_singular_matrix,
    reference_solve_mp,
    well_conditioned_matrix,
)
from tests.independent_checks import (  # noqa: E402
    independent_eta,
    independent_forward_error,
)

logging.disable(logging.WARNING)


def _line(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def _eta(a, x_np, b, n):
    return float(
        independent_eta(
            a,
            [mpf(float(x_np[i, 0])) for i in range(n)],
            [b[i, 0] for i in range(n)],
        )
    )


def demo_well_conditioned(cfg: Config) -> None:
    _line("1. WELL-CONDITIONED  (n=6, cond ~ 5)")
    a, b, _ = well_conditioned_matrix(6, seed=11)
    x32 = direct_low_precision_solve(a, b, "float32")
    x64 = direct_low_precision_solve(a, b, "float64")
    res = solve_system(a, b, cfg, "demo-well")
    xref = reference_solve_mp(a, b)
    x_col = [mpf(row[0]) for row in res.solution]
    print(f"  direct float32 backward error eta : {_eta(a, x32, b, 6):.3e}")
    print(f"  direct float64 backward error eta : {_eta(a, x64, b, 6):.3e}")
    print(
        "  refined backward error eta        : "
        f"{float(independent_eta(a, x_col, [b[i,0] for i in range(6)])):.3e}"
        f"  (accepted at {res.columns[0].accepted_at_stage} "
        f"iter {res.columns[0].accepted_at_iteration})"
    )
    print(
        "  refined forward error vs 150-dps ref: "
        f"{float(independent_forward_error(mp.matrix([[v] for v in x_col]), xref)):.3e}"
    )


def demo_ill_conditioned(cfg: Config) -> None:
    _line("2. SOLVABLE ILL-CONDITIONED  (n=8, cond ~ 1e10)")
    a, b, _, _ = conditioned_matrix(8, 10, seed=17)
    x32 = direct_low_precision_solve(a, b, "float32")
    x64 = direct_low_precision_solve(a, b, "float64")
    res = solve_system(a, b, cfg, "demo-ill")
    xref = reference_solve_mp(a, b)
    x_col = [mpf(row[0]) for row in res.solution]
    print(f"  log10 cond estimate               : {res.condition['log10_cond']}")
    print(f"  direct float32 forward error      : {float(independent_forward_error(x32, xref)):.3e}")
    print(f"  direct float64 forward error      : {float(independent_forward_error(x64, xref)):.3e}")
    print(
        "  refined forward error             : "
        f"{float(independent_forward_error(mp.matrix([[v] for v in x_col]), xref)):.3e}"
    )
    print(f"  refined backward error eta        : {res.columns[0].best_eta}")
    print(f"  decision / stage                  : {res.columns[0].status} / {res.columns[0].accepted_at_stage}")
    print(f"  precision floor (residual 60 dps) : {res.precision_floor_eta}")
    print("  -> float32 refinement saturates near cond*u32; ladder escalates to float64.")


def demo_singular(cfg: Config) -> None:
    _line("3. EXACT SINGULAR  (rank 2/3 integer matrix)")
    a, b = exact_singular_matrix()
    res = solve_system(a, b, cfg, "demo-singular")
    print(f"  status  : {res.status}")
    print(f"  reason  : {res.reason}")
    print(f"  solution: {res.solution}  (none returned)")
    for col in res.columns:
        print(f"  col {col.column}: {col.status}")


def demo_unreachable(cfg: Config) -> None:
    _line("4. UNREACHABLE TOLERANCE  (cond ~ 1e20, tol 1e-150, ladder <= 60 dps)")
    a, b, _, _ = conditioned_matrix(6, 20, seed=31)
    hard = cfg.with_overrides(
        {"backward_tol": "1e-150", "mp_dps_ladder": [20, 40, 60]}
    )
    res = solve_system(a, b, hard, "demo-unreachable")
    print(f"  status: {res.status}")
    print(f"  reason: {res.columns[0].reason}")
    print("  -> no convergence is claimed; the residual-precision floor is named.")


def main() -> None:
    with workprec(60):
        cfg = Config.load()
        demo_well_conditioned(cfg)
        demo_ill_conditioned(cfg)
        demo_singular(cfg)
        demo_unreachable(cfg)
    print("\nDone.")


if __name__ == "__main__":
    main()
