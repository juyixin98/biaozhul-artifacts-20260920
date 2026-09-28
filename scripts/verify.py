#!/usr/bin/env python3
"""Local verification harness.

Runs the whole backend over every synthetic fixture class:

* grid sparse, banded, dense-block (SPD)          -> solve vs dense reference
* natural vs RCM ordering                          -> fill / bandwidth report
* pattern-change pair                              -> cache reuse / rejection
* non-positive-definite fixtures                   -> localized pivot failure

Every run is logged to JSONL with a run_id and the dependency versions.  The
script exits non-zero if any checked criterion fails.

Usage::

    python scripts/verify.py                 # standard set
    python scripts/verify.py --quick         # small set only
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.settings import FactorizationConfig  # noqa: E402
from sparse_cholesky.api.logging_setup import (  # noqa: E402
    RunLogger,
    dependency_versions,
    new_run_id,
)
from sparse_cholesky.api.service import (  # noqa: E402
    ServiceFailure,
    analyze_and_solve,
)
from sparse_cholesky.core import FactorizationEngine  # noqa: E402
from sparse_cholesky.input.errors import (  # noqa: E402
    NonPositiveDefiniteError,
    SymbolicStructureMismatchError,
)
from sparse_cholesky.input.fixtures import (  # noqa: E402
    banded_spd,
    grid2d_laplacian,
    non_positive_definite_fixture,
    pattern_change_pair,
    spd_with_dense_block,
    tridiagonal_spd,
)
from sparse_cholesky.input.matrix import build_sparse_matrix  # noqa: E402

# Independent dense reference (not the sparse core under test).
DENSE_TOL = 1e-10


def dense_reference_solve(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return np.linalg.solve(a, b)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quick", action="store_true",
                        help="use only small fixtures")
    parser.add_argument("--log-dir", default=str(ROOT / "logs"))
    args = parser.parse_args()

    grid_side = 4 if args.quick else 8
    run_id = new_run_id("verify")
    logger = RunLogger(args.log_dir)
    logger.run_start(run_id, {"mode": "quick" if args.quick else "full"})

    print(f"run_id: {run_id}")
    print("versions:", json.dumps(dependency_versions(), sort_keys=True))
    print("=" * 78)

    engine = FactorizationEngine(FactorizationConfig())
    failures: list[str] = []

    def check(label: str, ok: bool, detail: str) -> None:
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {label}: {detail}")
        logger.event(run_id, label, "passed" if ok else "failed", detail=detail)
        if not ok:
            failures.append(label)

    spd_fixtures = [
        tridiagonal_spd(30),
        banded_spd(40, 4),
        grid2d_laplacian(grid_side),
        spd_with_dense_block(8, 6),
    ]

    # --- SPD cases: solution accuracy + fill / ordering effect -------------
    for fx in spd_fixtures:
        matrix = build_sparse_matrix(*fx.keys())
        dense_a = matrix.csc.toarray()
        rng = np.random.default_rng(123)
        x_true = rng.standard_normal(matrix.n)
        b = dense_a @ x_true
        for ordering in ("natural", "rcm"):
            t0 = time.time()
            outcome = analyze_and_solve(
                matrix, ordering=ordering, rhs=b,
                run_id=f"{run_id}-{fx.name}-{ordering}",
                engine=engine, logger=logger,
            )
            dt = time.time() - t0
            label = f"spd/{fx.name}/{ordering}"
            if isinstance(outcome, ServiceFailure):
                check(label, False, f"unexpected failure {outcome.error_type}")
                continue
            x = np.asarray(outcome.solution)
            x_ref = dense_reference_solve(dense_a, b)
            fwd = float(np.linalg.norm(x - x_ref, np.inf)
                        / np.linalg.norm(x_ref, np.inf))
            rel_res = outcome.evidence["residual"]["relative_residual_inf"]
            f = outcome.fill
            ok = fwd < DENSE_TOL and rel_res < DENSE_TOL
            detail = (
                f"fwd={fwd:.2e} resid={rel_res:.2e} "
                f"nnzL={f['nnz_l']} fill={f['fill_in']} "
                f"ratio={f['fill_ratio']:.2f} bw={f['bandwidth_before']}->"
                f"{f['bandwidth_after']} cache={outcome.cache_hit} "
                f"({dt:.2f}s)"
            )
            check(label, ok, detail)

    # --- Symbolic reuse across the pattern-change pair ---------------------
    a, bmat = pattern_change_pair()
    ma = build_sparse_matrix(*a.keys())
    mb = build_sparse_matrix(*bmat.keys())
    engine.factor(ma)
    second, _ = engine.factor(ma)
    check("cache/identical-pattern-hit", second.cache_hit,
          f"cache_hit={second.cache_hit}")
    changed, _ = engine.factor(mb)
    check("cache/changed-pattern-recomputed", not changed.cache_hit,
          f"cache_hit={changed.cache_hit} (must be False)")
    handle = engine.symbolic_for(ma)
    try:
        engine.factor(mb, symbolic_handle=handle)
        check("cache/forced-mismatch-rejected", False, "no error raised")
    except SymbolicStructureMismatchError:
        check("cache/forced-mismatch-rejected", True,
              "SymbolicStructureMismatchError raised")

    # --- Non-positive-definite localization --------------------------------
    for kind in ("zero_pivot", "negative_diag", "indefinite"):
        fx = non_positive_definite_fixture(kind, n=8)
        matrix = build_sparse_matrix(*fx.keys())
        try:
            engine.factor(matrix)
            check(f"nonpd/{kind}", False, "factorization unexpectedly succeeded")
        except NonPositiveDefiniteError as exc:
            ok = exc.pivot_index is not None and exc.ordering_index is not None
            check(
                f"nonpd/{kind}", ok,
                f"{exc.error_type} at permuted pivot {exc.pivot_index} "
                f"(original {exc.ordering_index}), value={exc.pivot_value:.3e}",
            )

    # --- Service must report failure, not success, for non-PD --------------
    fx = non_positive_definite_fixture("indefinite", n=6)
    outcome = analyze_and_solve(
        build_sparse_matrix(*fx.keys()),
        run_id=f"{run_id}-service-nonpd", logger=logger,
    )
    check("service/nonpd-not-success",
          isinstance(outcome, ServiceFailure)
          and outcome.error_type == "non_positive_definite_error",
          outcome.__class__.__name__)

    print("=" * 78)
    logger.run_end(run_id, "succeeded" if not failures else "failed",
                   failures=failures)
    if failures:
        print(f"{len(failures)} check(s) FAILED: {failures}")
        return 1
    print("ALL CHECKS PASSED")
    print(f"JSONL logs written under: {Path(args.log_dir).resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
