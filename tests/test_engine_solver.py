"""Engine-level acceptance tests.

Every numerical assertion is checked against an *independent* recomputation
(mpmath built-in LU at 150 dps, exact Fraction elimination, or a from-scratch
residual in ``tests/independent_checks.py``) - never against the engine's own
error module.
"""

from __future__ import annotations

import pytest

from app.config import Config
from app.numerical import mp, mpf, workprec
from app.numerical.engine import solve_system
from app.numerical.results import SINGULAR
from tests.fixtures import (
    conditioned_matrix,
    direct_low_precision_solve,
    exact_singular_matrix,
    reference_solve_fraction,
    reference_solve_mp,
    well_conditioned_matrix,
)
from tests.independent_checks import (
    independent_eta,
    independent_forward_error,
)

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def config() -> Config:
    return Config.load()


def _solution_column(result, j=0):
    return [mpf(row[j]) for row in result.solution]


# --------------------------------------------------------------------------- #
# Well-conditioned
# --------------------------------------------------------------------------- #


def test_well_conditioned_is_accepted_with_independent_backward_error(config):
    a, b, _ = well_conditioned_matrix(6, seed=11)
    result = solve_system(a, b, config, "unit-well")

    assert result.status == "accepted"
    assert result.columns[0].status == "accepted"
    x_col = _solution_column(result)
    eta = independent_eta(a, x_col, [b[i, 0] for i in range(6)])

    tol = mpf(config.backward_tol)
    # Independent recomputation, not the engine's reported figure.
    assert eta <= tol, f"independently measured eta {eta} exceeds tol {tol}"
    # The refined answer agrees with the independent 150-dps reference.
    x_ref = reference_solve_mp(a, b)
    fwd = independent_forward_error(mp.matrix([[v] for v in x_col]), x_ref)
    assert fwd < mpf("1e-12")


def test_well_conditioned_refinement_beats_direct_float32(config):
    """In the regime cond*u32 << 1, refinement from fp32 beats a raw fp32 solve."""
    a, b, _ = well_conditioned_matrix(6, seed=11)
    result = solve_system(a, b, config, "unit-well-refine")

    x_direct = direct_low_precision_solve(a, b, "float32")
    eta_direct = independent_eta(
        a,
        [mpf(float(x_direct[i, 0])) for i in range(6)],
        [b[i, 0] for i in range(6)],
    )
    eta_refined = independent_eta(
        a, _solution_column(result), [b[i, 0] for i in range(6)]
    )
    # Refinement drives the fp32-started solve several orders below the raw
    # fp32 backward error floor (~u32 ~= 1.2e-7).
    assert eta_refined < eta_direct / mpf(1000)


# --------------------------------------------------------------------------- #
# Solvable ill-conditioned
# --------------------------------------------------------------------------- #


def test_ill_conditioned_solvable_escalates_and_matches_reference(config):
    # cond ~ 1e10: a direct float32 solve loses ~7-10 forward digits, but the
    # staged solver should escalate float32 -> float64 and accept.
    a, b, _, singulars = conditioned_matrix(8, 10, seed=17)
    result = solve_system(a, b, config, "unit-ill10")

    assert result.status == "accepted"
    used = [s.stage for s in result.stages if s.used]
    assert "float64" in used  # float32 alone is insufficient -> escalation

    x_col = _solution_column(result)
    eta = independent_eta(a, x_col, [b[i, 0] for i in range(8)])
    assert eta <= mpf(config.backward_tol)

    x_ref = reference_solve_mp(a, b)
    fwd = independent_forward_error(mp.matrix([[v] for v in x_col]), x_ref)
    # Forward accuracy cannot beat cond * eta; assert it sits in the predicted
    # band (1e10 * 1e-12 = 1e-2) rather than pretending to be exact.
    assert fwd < mpf("1e-2")
    assert result.columns[0].forward_bound not in ("", "0.0")


def test_refinement_regime_boundary_vs_direct_solves(config):
    """Characterize WHEN refinement is more accurate than direct low precision.

    At cond ~ 1e4 (well below 1/u32 ~= 8.4e6), fp32-based refinement improves
    the forward error by many orders over a raw fp32 solve; at cond ~ 1e8
    (above 1/u32) fp32 refinement cannot converge and precision must escalate.
    """
    # Regime where low-precision refinement genuinely helps (cond ~ 1e4).
    a, b, _, _ = conditioned_matrix(8, 4, seed=23)
    result = solve_system(a, b, config, "unit-regime-good")
    x_direct32 = direct_low_precision_solve(a, b, "float32")
    fwd_direct = independent_forward_error(x_direct32, reference_solve_mp(a, b))
    fwd_refined = independent_forward_error(
        mp.matrix([[v] for v in _solution_column(result)]),
        reference_solve_mp(a, b),
    )
    assert fwd_refined < fwd_direct / mpf(1000)
    assert result.columns[0].accepted_at_stage == "float32"

    # Regime past the fp32 refinement limit: must escalate, not report fp32.
    a2, b2, _, _ = conditioned_matrix(8, 8, seed=29)
    result2 = solve_system(a2, b2, config, "unit-regime-escalate")
    assert result2.status == "accepted"
    assert result2.columns[0].accepted_at_stage in ("float64",)
    fp32_only = config.with_overrides(
        {
            "use_fp32_first": True,
            "use_fp64": False,
            "mp_dps_ladder": [],
            "backward_tol": "1e-12",
            "max_iterations_per_stage": 6,
        }
    )
    stuck = solve_system(a2, b2, fp32_only, "unit-regime-stuck")
    # No false convergence claim when the only stage stagnates.
    assert stuck.status == "not_met"
    assert stuck.columns[0].status == "not_met"


# --------------------------------------------------------------------------- #
# Singular
# --------------------------------------------------------------------------- #


def test_exact_singular_matrix_is_rejected_not_solved(config):
    a, b = exact_singular_matrix()
    result = solve_system(a, b, config, "unit-singular")

    assert result.status == SINGULAR
    assert result.solution is None
    assert all(c.status == "skipped_numerically_singular" for c in result.columns)
    # Evidence must include the rank deficiency and its basis.
    assert "rank 2/3" in result.reason
    assert result.stages[0].factor_pivot_ratio == "0.0"


def test_exact_rational_systems_agree_with_fraction_reference(config):
    """Independent exact-Fraction reference (no floating point at all)."""
    a_rows = [[3, 2, -1], [2, -2, 4], [-1, 1, -1]]
    b_rows = [1, -2, 0]
    exact_x = reference_solve_fraction(a_rows, b_rows)
    a = mp.matrix([[mpf(v) for v in row] for row in a_rows])
    b = mp.matrix([[mpf(v)] for v in b_rows])
    # Demand 25 correct digits. Iterative refinement reaches them even from an
    # fp32 factorization because residuals are formed at 60 dps (IR floor set
    # by residual precision, not factorization precision) for this benign cond.
    cfg = config.with_overrides({"backward_tol": "1e-25"})
    result = solve_system(a, b, cfg, "unit-fraction")
    assert result.status == "accepted"
    for i, exact in enumerate(exact_x):
        got = mpf(result.solution[i][0])
        assert abs(got - mpf(exact.numerator) / mpf(exact.denominator)) < mpf("1e-25")


# --------------------------------------------------------------------------- #
# Honesty: tolerance that cannot be reached must be reported, not converged
# --------------------------------------------------------------------------- #


def test_unreachable_tolerance_is_reported_not_claimed(config):
    a, b, _, _ = conditioned_matrix(6, 20, seed=31)
    # Ask for 1e-150 while residual evaluation only has 60 dps and the ladder
    # stops at 60 dps: the engine must say NOT MET and name the floor.
    cfg = config.with_overrides(
        {"backward_tol": "1e-150", "mp_dps_ladder": [20, 40, 60]}
    )
    result = solve_system(a, b, cfg, "unit-unreachable")
    assert result.status == "not_met"
    col = result.columns[0]
    assert col.status == "not_met"
    assert col.accepted_at_stage is None
    assert "No convergence reported" in col.reason
    # It must explain the residual-precision floor rather than failing silently.
    assert "residual" in col.reason.lower() or "floor" in col.reason.lower()


def test_solver_does_not_mutate_original_inputs(config):
    a, b, _ = well_conditioned_matrix(4, seed=5)
    a_before = [[a[i, j] for j in range(4)] for i in range(4)]
    solve_system(a, b, config, "unit-immutable")
    for i in range(4):
        for j in range(4):
            assert a[i, j] == a_before[i][j]


def test_no_usable_factorization_stage_is_reported_not_crashed(config):
    # Ill-conditioned enough to defeat very low mp rungs (4-5 dps), with all
    # binary stages disabled: the engine reports not_met, returns no solution.
    a, b, _, _ = conditioned_matrix(8, 12, seed=5)
    cfg = config.with_overrides(
        {"use_fp32_first": False, "use_fp64": False, "mp_dps_ladder": [4, 5]}
    )
    result = solve_system(a, b, cfg, "unit-no-factor")
    assert result.status == "not_met"
    assert result.solution is None
    assert "no usable factorization" in result.reason
    assert all(c.status == "not_met" for c in result.columns)
    assert all(not s.used for s in result.stages)


# --------------------------------------------------------------------------- #
# Condition number evidence
# --------------------------------------------------------------------------- #


def test_condition_estimate_matches_independent_svd(config):
    a, _, _, singulars = conditioned_matrix(6, 8, seed=41)
    with workprec(120):
        svals = sorted(abs(s) for s in mp.svd(a, compute_uv=False))
        true_cond = svals[-1] / svals[0]
    result = solve_system(a, _rhs_for(a), config, "unit-cond")
    reported = mpf(result.condition["svd_cond"])
    # The SVD (2-norm) corroboration should agree to a few percent.
    assert reported / true_cond < mpf("1.1")
    assert true_cond / reported < mpf("1.1")
    # The 1-norm estimate is a different norm; it may differ from the 2-norm
    # condition by up to a factor n (~ one log10 unit), assert that band only.
    assert abs(result.condition["log10_cond"] - 8.0) < 1.0


def _rhs_for(a):
    n = a.rows
    return mp.matrix([[mpf(1)] if i == 0 else [mpf(0)] for i in range(n)])
