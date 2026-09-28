#!/usr/bin/env python3
"""Local verification / demonstration driver.

Runs every built-in synthetic fixture through the full pipeline and
prints a table of:

* ordering and fill (nnz of L vs original lower triangle);
* residual, reconstruction and independent-reference agreement;
* failure category and failing pivot for the non-SPD fixtures.

It also compares all three orderings on the grid fixture and writes a
machine-readable JSON report to reports/verification.json.

Usage:
    python3 scripts/demo.py
"""
from __future__ import annotations

import json
import platform
import sys
import time
from importlib.metadata import version
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.numerical_input import fixtures  # noqa: E402
from app.service.engine import FactorizationEngine  # noqa: E402
from app.errors import SparseSpdError  # noqa: E402


def _versions() -> dict:
    out = {"python": platform.python_version()}
    for pkg in ("numpy", "scipy", "mpmath", "fastapi"):
        try:
            out[pkg] = version(pkg)
        except Exception:
            out[pkg] = "unknown"
    return out


def main() -> int:
    eng = FactorizationEngine(enable_cache=True)
    report: dict = {"versions": _versions(), "runs": []}

    print("=" * 78)
    print("SPD fixtures (factorize + solve + independent evidence)")
    print("=" * 78)
    spd_fixtures = [
        fixtures.grid_laplacian(7, 7),
        fixtures.banded(60),
        fixtures.arrowhead(30),
    ]
    for fx in spd_fixtures:
        t0 = time.perf_counter()
        r = eng.solve(fx.n, fx.rows, fx.cols, fx.vals, fx.rhs,
                      ordering="minimum_degree", with_mpmath=True)
        dt = time.perf_counter() - t0
        e = r.evidence
        print(f"\n[{fx.name}] {fx.description}")
        print(f"  n={fx.n} nnz_in={fx.vals.size} "
              f"nnz_L={r.report.nnz_l} fill={r.report.fill_entries} "
              f"ratio={r.report.fill_ratio:.2f} "
              f"reused={r.report.pattern_reused}")
        mp = e.mpmath_solution_error
        mp_txt = "n/a(n>60)" if mp is None else f"{mp:.2e}"
        print(f"  residual_rel={e.residual_rel:.2e} "
              f"reconstruct={e.reconstruction_abs:.2e} "
              f"vs_LAPACK={e.dense_solution_error:.2e} "
              f"vs_mpmath={mp_txt}")
        print(f"  evidence_passed={e.passed}  ({dt*1000:.0f} ms)")
        report["runs"].append({
            "fixture": fx.name, "n": fx.n, "spd": True,
            "nnz_l": r.report.nnz_l, "fill": r.report.fill_entries,
            "fill_ratio": r.report.fill_ratio,
            "residual_rel": e.residual_rel,
            "reconstruction": e.reconstruction_abs,
            "vs_lapack": e.dense_solution_error,
            "vs_mpmath": e.mpmath_solution_error,
            "passed": e.passed,
        })

    print("\n" + "=" * 78)
    print("Non-SPD / singular fixtures (must be classified, never 'ok')")
    print("=" * 78)
    for fx in (fixtures.negative_diagonal(),
               fixtures.indefinite_3x3(),
               fixtures.singular_matrix()):
        try:
            eng.solve(fx.n, fx.rows, fx.cols, fx.vals, fx.rhs,
                      ordering="natural", with_mpmath=False)
            outcome = {"fixture": fx.name, "status": "WRONGLY_SUCCEEDED"}
            print(f"\n[{fx.name}] UNEXPECTED SUCCESS (BUG)")
        except SparseSpdError as exc:
            ok_pivot = exc.original_index == fx.expected_bad_pivot
            print(f"\n[{fx.name}] {exc.code.value}: {exc}")
            print(f"  failing original index={exc.original_index} "
                  f"(expected {fx.expected_bad_pivot}, "
                  f"match={ok_pivot}) pivot={exc.pivot_value:.3e}")
            outcome = {
                "fixture": fx.name, "status": "error",
                "error_code": exc.code.value,
                "original_index": exc.original_index,
                "expected_index": fx.expected_bad_pivot,
                "pivot_correct": ok_pivot,
                "pivot_value": exc.pivot_value,
            }
        report["runs"].append(outcome)

    print("\n" + "=" * 78)
    print("Ordering / fill comparison on a 20x15 grid")
    print("=" * 78)
    gx = fixtures.grid_laplacian(20, 15)
    rows = eng.compare_orderings(gx.n, gx.rows, gx.cols, gx.vals)
    report["ordering_compare"] = rows
    print(f"{'ordering':<20}{'nnz_L':>10}{'fill':>10}{'ratio':>10}")
    for row in rows:
        print(f"{row['method']:<20}{row['nnz_l']:>10}"
              f"{row['fill_entries']:>10}{row['fill_ratio']:>10.2f}")

    out_dir = Path(__file__).resolve().parents[1] / "reports"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / "verification.json"
    out_path.write_text(json.dumps(report, indent=2,
                                   default=lambda o: float(o)
                                   if isinstance(o, np.floating) else str(o)))
    print(f"\nWrote {out_path}")

    # Non-zero exit if anything regressed.
    bad = [r for r in report["runs"]
           if (r.get("spd") and not r.get("passed"))
           or r.get("status") == "WRONGLY_SUCCEEDED"]
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
