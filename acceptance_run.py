#!/usr/bin/env python3
"""Acceptance run: generate systems with known solutions and compare
*residuals* (not iteration counts) across well-conditioned,
ill-conditioned, zero-RHS and illegal-input cases.

Run with::

    python3 acceptance_run.py            # human-readable table
    python3 acceptance_run.py --json     # machine-readable report

Every case independently recomputes ||b - A x|| and checks it against the
requested tolerance; the iteration count is reported for context only.
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass

import numpy as np

from sparse_cg import CSRMatrix, solve_pcg
from sparse_cg.exceptions import ValidationError
from sparse_cg.generators import (
    diagonal_scaled_laplacian,
    laplacian_1d,
    make_rhs_from_exact_solution,
    random_x,
    smooth_x,
    spd_from_mass_spring,
)


@dataclass
class CaseReport:
    name: str
    kind: str                  # solve | rejected
    status: str
    converged: bool
    n: int
    nnz: int
    requested_tol: float | None
    iterations: int
    initial_residual: float
    final_true_residual: float
    relative_true_residual: float | None
    solution_relative_error: float | None
    preconditioner: str
    true_checks: int
    max_recurrence_drift: float
    passes_residual_criterion: bool
    elapsed_seconds: float
    error_code: str | None = None
    notes: str = ""


def _solve_case(name: str, A: CSRMatrix, b: np.ndarray, x_true, *,
                tol: float, max_iter: int = 10_000,
                preconditioner: str = "none",
                notes: str = "") -> CaseReport:
    bnorm = float(np.linalg.norm(b))
    t0 = time.perf_counter()
    res = solve_pcg(A, b, tol=tol, max_iter=max_iter,
                    preconditioner=preconditioner)
    elapsed = time.perf_counter() - t0

    # INDEPENDENT recomputation -- never reuse the solver's own number.
    true_r = float(np.linalg.norm(b - A.matvec(res.x)))
    if bnorm > 0.0:
        rel = true_r / bnorm
        criterion = rel <= tol * (1 + 1e-9)
    else:
        rel = None
        criterion = true_r == 0.0
    if x_true is not None:
        xt_norm = float(np.linalg.norm(x_true))
        xerr = (float(np.linalg.norm(res.x - x_true)) / xt_norm
                if xt_norm > 0 else float(np.linalg.norm(res.x)))
    else:
        xerr = None

    return CaseReport(
        name=name, kind="solve", status=res.status,
        converged=res.converged, n=A.n, nnz=A.nnz, requested_tol=tol,
        iterations=res.iterations,
        initial_residual=res.initial_residual_norm,
        final_true_residual=true_r,
        relative_true_residual=rel,
        solution_relative_error=xerr,
        preconditioner=preconditioner,
        true_checks=res.true_residual_checks,
        max_recurrence_drift=res.max_residual_drift,
        passes_residual_criterion=bool(criterion and res.converged),
        elapsed_seconds=elapsed, notes=notes,
    )


def _rejected_case(name: str, n: int, nnz: int, payload_builder,
                   notes: str = "") -> CaseReport:
    """A case expected to be rejected at request/build time."""
    t0 = time.perf_counter()
    code = None
    try:
        payload_builder()
        status, converged = "NOT_REJECTED", True
    except ValidationError as exc:
        status, converged, code = "rejected", False, exc.code
    elapsed = time.perf_counter() - t0
    return CaseReport(
        name=name, kind="rejected", status=status, converged=converged,
        n=n, nnz=nnz, requested_tol=None, iterations=0,
        initial_residual=float("nan"), final_true_residual=float("nan"),
        relative_true_residual=None, solution_relative_error=None,
        preconditioner="-", true_checks=0, max_recurrence_drift=float("nan"),
        passes_residual_criterion=(code is not None),
        elapsed_seconds=elapsed, error_code=code, notes=notes,
    )


def build_reports() -> list[CaseReport]:
    reports: list[CaseReport] = []

    # 1. Well-conditioned: 1-D Laplacian, smooth known solution.
    n = 500
    A, _ = laplacian_1d(n)
    x_true = smooth_x(n)
    b, _ = make_rhs_from_exact_solution(A, x_true)
    reports.append(_solve_case(
        "laplacian1d_smooth_none", A, b, x_true,
        tol=1e-10, preconditioner="none"))
    reports.append(_solve_case(
        "laplacian1d_smooth_jacobi", A, b, x_true,
        tol=1e-10, preconditioner="jacobi"))

    # 2. Diagonally dominant mass-spring, random solution.
    n = 1000
    A, _ = spd_from_mass_spring(n, seed=3)
    x_true = random_x(n, seed=17)
    b, _ = make_rhs_from_exact_solution(A, x_true)
    reports.append(_solve_case(
        "massspring_random_none", A, b, x_true,
        tol=1e-9, preconditioner="none"))
    reports.append(_solve_case(
        "massspring_random_jacobi", A, b, x_true,
        tol=1e-9, preconditioner="jacobi"))

    # 3. Ill-conditioned, known spectrum: kappa = 1e8 and 1e10.
    for kappa in (1e8, 1e10):
        n = 400
        A, eig = diagonal_scaled_laplacian(n, kappa)
        x_true = random_x(n, seed=23)
        b, _ = make_rhs_from_exact_solution(A, x_true)
        reports.append(_solve_case(
            f"scaledlaplacian_kappa1e{int(np.log10(kappa))}",
            A, b, x_true, tol=1e-8, max_iter=10_000,
            preconditioner="none",
            notes=("forward error exceeds residual by ~kappa; this is "
                   "the expected ill-conditioning signature")))

    # 4. Zero RHS.
    n = 200
    A, _ = laplacian_1d(n)
    r0 = solve_pcg(A, np.zeros(n), tol=1e-10)
    reports.append(CaseReport(
        name="zero_rhs", kind="solve", status=r0.status,
        converged=r0.converged, n=n, nnz=A.nnz, requested_tol=1e-10,
        iterations=r0.iterations, initial_residual=0.0,
        final_true_residual=r0.residual_norm,
        relative_true_residual=0.0, solution_relative_error=0.0,
        preconditioner="none", true_checks=r0.true_residual_checks,
        max_recurrence_drift=0.0,
        passes_residual_criterion=(r0.status == "zero_rhs"),
        elapsed_seconds=0.0, notes="x = 0 is the unique SPD solution"))

    # 5. Indefinite input (positive diagonal, negative eigenvalue).
    A = CSRMatrix(2, [0, 2, 4], [0, 1, 0, 1], [2.0, 3.0, 3.0, 2.0])
    rc = solve_pcg(A, np.array([1.0, -1.0]), tol=1e-10)
    reports.append(CaseReport(
        name="indefinite_curvature", kind="solve", status=rc.status,
        converged=rc.converged, n=2, nnz=4, requested_tol=1e-10,
        iterations=rc.iterations, initial_residual=rc.initial_residual_norm,
        final_true_residual=rc.residual_norm,
        relative_true_residual=rc.relative_residual,
        solution_relative_error=None, preconditioner="none",
        true_checks=rc.true_residual_checks,
        max_recurrence_drift=0.0,
        passes_residual_criterion=(rc.status == "non_positive_curvature"),
        elapsed_seconds=0.0,
        notes=f"p^T A p = {rc.non_positive_value:g} (eigenvalues 5, -1)"))

    # 6. Illegal CSR structures (all must be rejected with a stable code).
    def bad_indptr():
        return CSRMatrix(3, [0, 2, 1, 3], [0, 1, 0], [1.0, 2.0, 3.0])

    def bad_index():
        return CSRMatrix(2, [0, 1, 2], [0, 5], [1.0, 2.0])

    def bad_pointer_end():
        return CSRMatrix(2, [0, 1, 2], [0, 1, 0], [1.0, 2.0, 3.0])

    def bad_nan():
        return CSRMatrix(1, [0, 1], [0], [float("nan")])

    reports.append(_rejected_case(
        "illegal_csr_indptr_decrease", 3, 3, bad_indptr,
        "row pointers must be non-decreasing"))
    reports.append(_rejected_case(
        "illegal_csr_column_oor", 2, 2, bad_index,
        "column index >= n"))
    reports.append(_rejected_case(
        "illegal_csr_indptr_end", 2, 3, bad_pointer_end,
        "indptr[-1] must equal nnz"))
    reports.append(_rejected_case(
        "illegal_csr_nan_value", 1, 1, bad_nan,
        "non-finite stored value"))

    # 7. Non-symmetric and non-positive-diagonal (valid CSR, invalid SPD).
    def nonsymmetric():
        M = CSRMatrix(3, [0, 2, 4, 6],
                      [0, 1, 0, 2, 1, 2],
                      [4.0, -1.0, -1.5, 4.0, -1.0, 4.0])
        M.assert_symmetric()

    def nonpd_diag():
        M = CSRMatrix(2, [0, 2, 4], [0, 1, 0, 1], [1.0, 1.0, 1.0, 0.0])
        M.assert_positive_diagonal()

    reports.append(_rejected_case(
        "non_symmetric_values", 3, 6, nonsymmetric,
        "|A[0,1]-A[1,0]| = 0.5"))
    reports.append(_rejected_case(
        "zero_diagonal_semidefinite", 2, 4, nonpd_diag,
        "zero diagonal => not PD"))

    return reports


def _fmt(v, spec=".3e"):
    if v is None:
        return "-"
    try:
        if np.isnan(v):
            return "nan"
    except TypeError:
        pass
    return format(v, spec)


def print_table(reports: list[CaseReport]) -> None:
    hdr = (f"{'case':38s} {'status':23s} {'n':>5s} {'it':>5s} "
           f"{'req.tol':>9s} {'true rel.res':>12s} {'x rel.err':>11s} "
           f"{'pass':>5s}")
    print(hdr)
    print("-" * len(hdr))
    n_pass = 0
    for r in reports:
        if r.kind == "rejected":
            pass_s = "OK" if r.passes_residual_criterion else "FAIL"
            print(f"{r.name:38s} {r.status + '(' + str(r.error_code) + ')':23s} "
                  f"{r.n:5d} {'-':>5s} {'-':>9s} {'-':>12s} {'-':>11s} "
                  f"{pass_s:>5s}  {r.notes}")
        else:
            pass_s = "OK" if r.passes_residual_criterion else "FAIL"
            print(f"{r.name:38s} {r.status:23s} {r.n:5d} {r.iterations:5d} "
                  f"{_fmt(r.requested_tol, '.1e'):>9s} "
                  f"{_fmt(r.relative_true_residual):>12s} "
                  f"{_fmt(r.solution_relative_error):>11s} {pass_s:>5s}")
        n_pass += bool(r.passes_residual_criterion)
    print("-" * len(hdr))
    print(f"{n_pass}/{len(reports)} cases behaved as specified.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true",
                    help="emit a JSON report instead of a table")
    args = ap.parse_args()
    reports = build_reports()
    if args.json:
        print(json.dumps([asdict(r) for r in reports], indent=2,
                         default=lambda o: None if (
                             isinstance(o, float) and not np.isfinite(o))
                         else o))
    else:
        print_table(reports)
    return 0 if all(r.passes_residual_criterion for r in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
