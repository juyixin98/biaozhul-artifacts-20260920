#!/usr/bin/env python3
"""Local end-to-end demonstration of the eigendecomposition service.

Runs entirely offline on synthetic matrices and prints, per case:
verdict, concrete eigenvalues, independent residual/reconstruction errors,
degenerate-cluster handling, and the processing trace/location.

Usage:
    python scripts/demo.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from sym_eig.config import Settings  # noqa: E402
from sym_eig.service.engine import RequestOptions, run_eigendecomposition  # noqa: E402


def _spectrum(eigenvalues, seed):
    rng = np.random.default_rng(seed)
    q = np.linalg.qr(rng.standard_normal((len(eigenvalues),) * 2))[0]
    return (q * eigenvalues) @ q.T


def _print_result(title, result):
    print("=" * 72)
    print(title)
    print("-" * 72)
    print(f"request_id : {result.request_id}")
    print(f"verdict    : {result.verdict}")
    if result.error_category:
        print(f"category   : {result.error_category}")
        print(f"message    : {result.error_message}")
        print(f"details    : {json.dumps(result.error_details, indent=2)}")
    if result.eigenvalues is not None:
        shown = result.eigenvalues[:8]
        extra = "" if len(result.eigenvalues) <= 8 else f" ... ({len(result.eigenvalues)} total)"
        print(f"eigenvalues: {[round(x, 10) for x in shown]}{extra}")
        print(f"clusters   : {[c['multiplicity'] for c in result.clusters]}")
        ev = result.evidence
        print(
            "evidence   : "
            f"residual={ev['residual_relative_fro']:.3e} "
            f"orthogonality={ev['orthogonality_fro']:.3e} "
            f"reconstruction={ev['reconstruction_relative_fro']:.3e}"
        )
        if result.reference_comparison:
            rc = result.reference_comparison
            print(
                "reference  : "
                f"{rc['source']} "
                f"eigenvalue_err={rc['max_eigenvalue_abs_error']:.3e} "
                f"subspace_sin={rc['max_sin_principal_angle']:.3e} "
                f"(degenerate subspaces: {rc['degenerate_clusters_compared_as_subspaces']})"
            )
        print("gates      :")
        for gate in result.gates:
            mark = "PASS" if gate["passed"] else "FAIL"
            print(f"             [{mark}] {gate['name']}: "
                  f"{gate['value']:.3e} <= {gate['threshold']:.0e}")
        if result.uncertainties:
            print("uncertain  :")
            for u in result.uncertainties:
                print(f"             - {u}")
        if result.limitations:
            print("limitations:")
            for note in result.limitations:
                print(f"             - {note}")
        comp = result.computation
        print(
            "compute    : "
            f"householder_steps={comp['householder_steps']} "
            f"qr_sweeps={comp['qr_sweeps_total']} "
            f"max_block_sweeps={comp['qr_sweeps_max_per_block']} "
            f"budget={comp['sweep_budget']}"
        )
    print("trace      :")
    for step in result.trace:
        print(f"             {step['step']} ({step['elapsed_ms']:.2f} ms)")
    loc = result.processing_location
    print(
        "processed  : "
        f"service v{loc['service_version']} on {loc['host']} "
        f"(python {loc['python']}, numpy {loc['numpy']}, "
        f"scipy {loc['scipy']}, mpmath {loc['mpmath']})"
    )
    print()


def main() -> None:
    settings = Settings(max_n=256, max_iters=30, reference_dps=50,
                        reference_max_n=24)

    diagonal = np.diag([-3.0, -1.0, 0.5, 2.0, 7.0])
    repeated = _spectrum([1.0, 1.0, 1.0, 2.0, 3.5, 3.5, 5.0], seed=101)
    near_degenerate = _spectrum(
        [1.0, 1.0 + 1e-12, 4.0, 4.0 + 2e-13, 9.0], seed=202
    )
    wide_scales = np.diag([-1.0e8, 1.0, 1.0e-6])

    _print_result(
        "CASE 1 - diagonal matrix",
        run_eigendecomposition(diagonal.tolist(), settings,
                               RequestOptions(request_id="demo-diagonal")),
    )
    _print_result(
        "CASE 2 - repeated spectrum (subspace comparison)",
        run_eigendecomposition(repeated.tolist(), settings,
                               RequestOptions(request_id="demo-repeated")),
    )
    _print_result(
        "CASE 3 - near-degenerate eigenvalues",
        run_eigendecomposition(near_degenerate.tolist(), settings,
                               RequestOptions(request_id="demo-near-degen")),
    )
    _print_result(
        "CASE 4 - widely separated scales",
        run_eigendecomposition(wide_scales.tolist(), settings,
                               RequestOptions(request_id="demo-scales")),
    )

    # Failure classification: an asymmetric input.
    _print_result(
        "CASE 5 - non-symmetric input (expected NON_SYMMETRIC)",
        run_eigendecomposition([[1.0, 2.0], [0.5, 1.0]], settings,
                               RequestOptions(request_id="demo-skew")),
    )

    # Failure classification: iteration budget exhausted, never fake success.
    n = 30
    off = np.ones(n - 1)
    laplacian = 2.0 * np.eye(n) - np.diag(off, 1) - np.diag(off, -1)
    _print_result(
        "CASE 6 - tiny iteration budget (expected NON_CONVERGENCE)",
        run_eigendecomposition(
            laplacian.tolist(), settings,
            RequestOptions(request_id="demo-budget", max_iters=1,
                           reference="none"),
        ),
    )


if __name__ == "__main__":
    main()
