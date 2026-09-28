"""Independent evidence: solution extraction, exact back-substitution and
floating-point near-singularity diagnosis.

Nothing here trusts the kernel's arithmetic.  Every claimed result is
re-derived from the *original* parsed system:

* parametric solutions are read off the RREF shape (particular vector plus a
  null-space basis indexed by free columns);
* inconsistency is proved by a left-null-space vector ``y`` (obtained from
  the kernel's witness rows, converted back to original-row coordinates) and
  then **independently re-checked**: ``y.A == 0`` and ``y.b != 0`` are
  recomputed here directly from the original ``A`` and ``b``;
* every particular vector and null-space vector is substituted into the
  original equations with fresh :class:`Fraction` loops.

The floating-point diagnosis is deliberately *separate and labelled*:
NumPy/SciPy evaluate the same system in IEEE-754 float64 and mpmath at high
precision, so a test can prove that an exact answer is indistinguishable to
double precision yet resolved exactly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction

import numpy as np

from .kernel import EliminationResult

UNIQUE = "unique"
INFINITE = "infinite"
INCONSISTENT = "inconsistent"


@dataclass
class RhsSolution:
    index: int
    classification: str
    rank_augmented: int
    particular: list[Fraction] | None = None
    nullspace_basis: list[list[Fraction]] = field(default_factory=list)
    witness_row: int | None = None
    left_null_vector: list[Fraction] | None = None  # in ORIGINAL row coordinates
    y_dot_b: Fraction | None = None


@dataclass
class VerificationReport:
    """Independent exact residuals, recomputed from the original A and b."""

    particular_residuals: list[list[str]]   # per rhs, per equation
    nullspace_residuals: list[list[list[str]]]  # per rhs, per basis vector
    witness_y_dot_A: list[list[str | None]]  # per rhs, per equation (None if N/A)
    witness_y_dot_b: list[str | None]
    all_residuals_zero: bool


def extract_solutions(
    res: EliminationResult,
    b: list[list[Fraction]],
) -> list[RhsSolution]:
    """Read parametric solutions / contradictions out of the augmented RREF.

    ``res.rref`` has width ``n + rhs_count``.  Pivot rows are normalized with
    pivot value 1.  Free columns parameterize the null space.
    """
    m, n, rank = res.rows, res.cols, res.rank
    k = len(b[0])
    pivot_cols = set(res.pivot_columns)
    pivot_of_row = {r: c for r, c in res.pivots}
    free_cols = [c for c in range(n) if c not in pivot_cols]

    solutions: list[RhsSolution] = []
    for r in range(k):
        bc = n + r
        # A RREF row whose coefficient part is all zero is either 0=0 or a
        # contradiction 0 = (non-zero rhs).
        bad_row = None
        for i in range(m):
            if i in pivot_of_row:
                continue
            if res.rref[i][bc] != 0:
                bad_row = i
                break

        if bad_row is not None:
            y = _witness_in_original_coordinates(res, bad_row)
            solutions.append(RhsSolution(
                index=r,
                classification=INCONSISTENT,
                rank_augmented=rank + 1,
                witness_row=bad_row,
                left_null_vector=y,
                y_dot_b=None,  # filled by independent verification
            ))
            continue

        # Consistent.  Particular vector: free variables set to zero, pivot
        # variables read from the rhs column of their pivot row.
        x0 = [Fraction(0) for _ in range(n)]
        for pr, pc in res.pivots:
            x0[pc] = res.rref[pr][bc]

        basis: list[list[Fraction]] = []
        for f in free_cols:
            v = [Fraction(0) for _ in range(n)]
            v[f] = Fraction(1)
            for pr, pc in res.pivots:
                v[pc] = -res.rref[pr][f]
            basis.append(v)

        solutions.append(RhsSolution(
            index=r,
            classification=UNIQUE if not free_cols else INFINITE,
            rank_augmented=rank,
            particular=x0,
            nullspace_basis=basis,
        ))
    return solutions


def _witness_in_original_coordinates(
    res: EliminationResult, row: int
) -> list[Fraction]:
    """Return the left-combination vector for ``row`` in ORIGINAL coordinates.

    The kernel divides input row ``j`` by its content divisor ``g_j`` and
    initializes witness row ``j`` as ``e_j / g_j``; every later operation is
    mirrored linearly.  Consequently the invariant is

        RREF == P @ M_original  (the RAW, pre-content-reduction input),

    so a witness row ``p`` already satisfies ``p . A_original == 0`` directly -
    no further divisor conversion (dividing again would double-scale it).
    """
    return list(res.witness[row])


def independently_verify(
    A: list[list[Fraction]],
    b: list[list[Fraction]],
    solutions: list[RhsSolution],
) -> VerificationReport:
    """Recompute every residual directly from original A, b (own code path)."""
    m, n = len(A), len(A[0])
    k = len(b[0])

    def mat_vec(mat: list[list[Fraction]], x: list[Fraction]) -> list[Fraction]:
        return [
            sum((mat[i][j] * x[j] for j in range(len(x))), Fraction(0))
            for i in range(len(mat))
        ]

    def vec_sub(u: list[Fraction], v: list[Fraction]) -> list[Fraction]:
        return [a - c for a, c in zip(u, v)]

    all_zero = True
    p_res: list[list[str]] = []
    n_res: list[list[list[str]]] = []
    y_res: list[list[str | None]] = []
    yb_res: list[str | None] = []

    for r, sol in enumerate(solutions):
        br = [b[i][r] for i in range(m)]

        if sol.classification == INCONSISTENT:
            p_res.append([])
            n_res.append([])
            assert sol.left_null_vector is not None
            yta = _y_times_A(A, sol.left_null_vector)
            ytb = sum(
                (sol.left_null_vector[i] * br[i] for i in range(m)), Fraction(0)
            )
            sol.y_dot_b = ytb
            y_res.append([str(v) for v in yta])
            yb_res.append(str(ytb))
            if any(v != 0 for v in yta) or ytb == 0:
                all_zero = False
            continue

        y_res.append([None] * n)
        yb_res.append(None)

        assert sol.particular is not None
        resid = vec_sub(mat_vec(A, sol.particular), br)
        p_res.append([str(v) for v in resid])
        if any(v != 0 for v in resid):
            all_zero = False

        bres: list[list[str]] = []
        for v in sol.nullspace_basis:
            nr = mat_vec(A, v)
            bres.append([str(x) for x in nr])
            if any(x != 0 for x in nr):
                all_zero = False
        n_res.append(bres)

    return VerificationReport(
        particular_residuals=p_res,
        nullspace_residuals=n_res,
        witness_y_dot_A=y_res,
        witness_y_dot_b=yb_res,
        all_residuals_zero=all_zero,
    )


def _y_times_A(
    A: list[list[Fraction]], y: list[Fraction]
) -> list[Fraction]:
    """Independent row-vector times matrix: ``y^T A`` (own loops)."""
    m, n = len(A), len(A[0])
    out = [Fraction(0) for _ in range(n)]
    for i in range(m):
        yi = y[i]
        if yi == 0:
            continue
        for j in range(n):
            out[j] += yi * A[i][j]
    return out


# ---------------------------------------------------------------------------
# Floating-point diagnosis (explicitly approximate; never fed back into core)
# ---------------------------------------------------------------------------

def float64_diagnosis(
    A: list[list[Fraction]],
    b: list[list[Fraction]],
    exact_rank: int,
    solutions: list[RhsSolution],
) -> dict:
    """Compare the exact result against float64 (NumPy/SciPy) and high-precision
    mpmath views.  All numbers here are labelled approximations.
    """
    import mpmath
    from scipy.linalg import svdvals

    Af = np.array([[float(v) for v in row] for row in A], dtype=np.float64)
    Bf = np.array([[float(v) for v in col] for col in zip(*b)], dtype=np.float64)
    m, n = Af.shape
    eps = float(np.finfo(np.float64).eps)

    singular = svdvals(Af)
    s0 = float(singular[0]) if singular.size else 0.0
    smin = float(singular[-1]) if singular.size else 0.0
    rcond64 = smin / s0 if s0 > 0 else 0.0
    rank64 = int(np.linalg.matrix_rank(Af))

    per_rhs: list[dict] = []
    for r in range(Bf.shape[0]):
        br = Bf[r]
        x_ls, *_ = np.linalg.lstsq(Af, br, rcond=None)
        resid_vec = br - Af @ x_ls
        resid_norm = float(np.linalg.norm(resid_vec))
        scale = float(np.linalg.norm(br)) + 1.0
        rel = resid_norm / scale
        # Documented heuristic, not an exact verdict: at double precision a
        # residual above ~1e-10 relative is "apparently inconsistent".
        looks_inconsistent = rel > 1e-10
        exact = solutions[r].classification
        per_rhs.append({
            "index": r,
            "lstsq_relative_residual": repr(rel),
            "lstsq_residual_norm2": repr(resid_norm),
            "looks_inconsistent_at_double": looks_inconsistent,
            "exact_classification": exact,
        })

    double_confused = rank64 != exact_rank

    # High-precision mpmath view (80 decimal digits).  For square full-rank
    # systems, solve and measure the residual there.
    mp_block: dict = {"dps": 80}
    if m == n and exact_rank == n:
        mpmath.mp.dps = 80
        M = mpmath.matrix([[mpmath.mpf(str(v)) for v in row] for row in A])
        det_mp = mpmath.det(M)
        mp_block["det_mpmath_80dps"] = mpmath.nstr(det_mp, 25)
        mp_block["det_normalized_by_scale"] = mpmath.nstr(
            det_mp / (s0 ** n if s0 > 0 else mpmath.mpf(1)), 25
        )
        sols_mp = []
        for r in range(Bf.shape[0]):
            vr = mpmath.matrix([mpmath.mpf(str(b[i][r])) for i in range(m)])
            xm = mpmath.lu_solve(M, vr)
            rm = vr - M * xm
            sols_mp.append({
                "index": r,
                "max_abs_residual_80dps": mpmath.nstr(
                    max(abs(rm[i]) for i in range(m)), 10
                ),
            })
        mp_block["solutions"] = sols_mp
        mp_block["resolved_at_80dps"] = True
        if double_confused:
            mp_block["note"] = (
                "float64 cannot distinguish this matrix from a singular one, "
                "but 80-digit arithmetic resolves its exact structure"
            )

    return {
        "labelled_approximate": True,
        "machine_epsilon": repr(eps),
        "singular_values_float64": [repr(float(x)) for x in singular],
        "rcond_float64": repr(rcond64),
        "rank_float64": rank64,
        "rank_exact": exact_rank,
        "double_confuses_exact_structure": double_confused,
        "per_rhs": per_rhs,
        "mpmath": mp_block,
    }
