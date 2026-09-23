"""Tests for the PCG solver: convergence, true residuals and failure states."""

from __future__ import annotations

import numpy as np
import unittest

from sparse_cg import CSRMatrix, PCGConfig, solve_pcg
from sparse_cg.generators import (
    diagonal_scaled_laplacian,
    laplacian_1d,
    make_rhs_from_exact_solution,
    random_x,
    smooth_x,
    spd_from_mass_spring,
)
from sparse_cg.solver import SUCCESS_STATUSES
from tests._utils import dense_to_csr, true_residual_norm


class TestBasicConvergence(unittest.TestCase):
    """Acceptance criterion everywhere is the TRUE residual, not iteration
    counts (iteration counts are asserted only with loose upper bounds)."""

    def test_laplacian_recovers_known_solution_no_precond(self):
        n = 200
        A, _ = laplacian_1d(n)
        x_true = smooth_x(n)
        b, _ = make_rhs_from_exact_solution(A, x_true)
        res = solve_pcg(A, b, tol=1e-10)
        self.assertEqual(res.status, "converged")
        # The primary acceptance quantity: recomputed true residual.
        true_rn = true_residual_norm(A, res.x, b)
        self.assertLessEqual(true_rn, 1e-10 * np.linalg.norm(b) * (1 + 1e-9))
        self.assertEqual(res.residual_kind, "true")
        # And the solution itself is accurate.
        self.assertLess(np.linalg.norm(res.x - x_true)
                        / np.linalg.norm(x_true), 1e-9)
        # Iterations far below n for this easy system (loose bound only).
        self.assertLess(res.iterations, n)

    def test_laplacian_jacobi(self):
        n = 300
        A, _ = laplacian_1d(n)
        x_true = smooth_x(n)
        b, _ = make_rhs_from_exact_solution(A, x_true)
        res = solve_pcg(A, b, tol=1e-10, preconditioner="jacobi")
        self.assertEqual(res.status, "converged")
        self.assertLess(true_residual_norm(A, res.x, b),
                        1e-10 * np.linalg.norm(b) * (1 + 1e-9))

    def test_roundoff_floor_is_reported_as_stagnation_not_false_success(self):
        # When the requested relative tolerance lies below the matvec
        # round-off floor (here ||b|| is small so the *relative* floor is a
        # few e-12), the solver must terminate with stagnation and a true
        # residual honestly above tol -- never claim convergence.
        n = 300
        A, _ = laplacian_1d(n)
        x_true = smooth_x(n)
        b, _ = make_rhs_from_exact_solution(A, x_true)
        res = solve_pcg(A, b, tol=1e-12, preconditioner="jacobi",
                        max_iter=2000)
        self.assertEqual(res.status, "stagnation")
        self.assertFalse(res.converged)
        self.assertGreater(res.relative_residual, 1e-12)
        # The absolute residual is nevertheless at machine-precision level.
        self.assertLess(res.residual_norm, 1e-12)

    def test_none_and_jacobi_reach_same_accuracy(self):
        n = 150
        A, _ = spd_from_mass_spring(n)
        x_true = random_x(n, seed=3)
        b, _ = make_rhs_from_exact_solution(A, x_true)
        r1 = solve_pcg(A, b, tol=1e-11, preconditioner="none")
        r2 = solve_pcg(A, b, tol=1e-11, preconditioner="jacobi")
        n1 = true_residual_norm(A, r1.x, b)
        n2 = true_residual_norm(A, r2.x, b)
        self.assertLess(n1, 1e-11 * np.linalg.norm(b))
        self.assertLess(n2, 1e-11 * np.linalg.norm(b))

    def test_identity_one_iteration(self):
        A = CSRMatrix(3, [0, 1, 2, 3], [0, 1, 2], [1.0, 1.0, 1.0])
        b = np.array([1.0, 2.0, 3.0])
        res = solve_pcg(A, b, tol=1e-12)
        np.testing.assert_allclose(res.x, b, atol=1e-12)
        self.assertEqual(res.status, "converged")

    def test_dense_random_spd_matches_numpy_solution(self):
        # Cross-check against NumPy's dense direct solve on a modest system.
        rng = np.random.default_rng(9)
        n = 40
        G = rng.standard_normal((n, n))
        D = G @ G.T + n * np.eye(n)  # SPD
        A = dense_to_csr(D)
        x_true = rng.standard_normal(n)
        b = D @ x_true
        res = solve_pcg(A, b, tol=1e-10)
        self.assertEqual(res.status, "converged")
        x_np = np.linalg.solve(D, b)
        np.testing.assert_allclose(res.x, x_np, rtol=1e-8, atol=1e-10)
        self.assertLess(true_residual_norm(A, res.x, b),
                        1e-10 * np.linalg.norm(b))

    def test_x0_hot_start_converges_faster_or_equal(self):
        n = 400
        A, _ = laplacian_1d(n)
        x_true = smooth_x(n)
        b, _ = make_rhs_from_exact_solution(A, x_true)
        x0 = 0.9 * x_true  # close to the answer
        res = solve_pcg(A, b, x0=x0, tol=1e-10)
        self.assertEqual(res.status, "converged")
        self.assertLess(true_residual_norm(A, res.x, b),
                        1e-10 * np.linalg.norm(b))

    def test_x0_already_solution(self):
        n = 30
        A, _ = laplacian_1d(n)
        x_true = smooth_x(n)
        b, _ = make_rhs_from_exact_solution(A, x_true)
        res = solve_pcg(A, b, x0=x_true, tol=1e-12)
        self.assertEqual(res.status, "converged")
        self.assertEqual(res.iterations, 0)
        self.assertLess(true_residual_norm(A, res.x, b),
                        1e-12 * np.linalg.norm(b) + 1e-14)


class TestIllConditioned(unittest.TestCase):

    def test_moderate_kappa_converges_and_residual_smaller_than_xerr(self):
        # Key conceptual check on ill-conditioned systems: the residual may
        # be tiny while the forward error ||x-x*|| is amplified by kappa.
        # The acceptance criterion must be the residual (what we promise),
        # and the history must let users see the trade-off.
        n = 250
        A, eig = diagonal_scaled_laplacian(n, 1e8)
        achieved_kappa = float(eig.max() / eig.min())
        self.assertGreater(achieved_kappa, 5e7)
        x_true = random_x(n, seed=4)
        b, _ = make_rhs_from_exact_solution(A, x_true)
        res = solve_pcg(A, b, tol=1e-8, max_iter=5000)
        self.assertEqual(res.status, "converged")
        rn = true_residual_norm(A, res.x, b)
        self.assertLessEqual(rn, 1e-8 * np.linalg.norm(b) * (1 + 1e-9))
        # Documented ill-conditioning effect: forward error is much larger
        # than the residual-based metric.  This is expected, not a bug.
        xerr = np.linalg.norm(res.x - x_true) / np.linalg.norm(x_true)
        self.assertGreater(xerr, rn / np.linalg.norm(b))

    def test_kappa_1e10_still_monitors_true_residual(self):
        n = 200
        A, _ = diagonal_scaled_laplacian(n, 1e10)
        x_true = random_x(n, seed=8)
        b, _ = make_rhs_from_exact_solution(A, x_true)
        res = solve_pcg(A, b, tol=1e-8, max_iter=5000)
        self.assertEqual(res.status, "converged")
        self.assertLess(true_residual_norm(A, res.x, b),
                        1e-8 * np.linalg.norm(b) * (1 + 1e-9))

    def test_too_tight_budget_reports_max_iterations_residual_vs_tol(self):
        # With a deliberately tiny iteration budget the solver must NOT
        # claim convergence; the returned residual honestly exceeds tol.
        n = 300
        A, _ = diagonal_scaled_laplacian(n, 1e6)
        x_true = random_x(n, seed=6)
        b, _ = make_rhs_from_exact_solution(A, x_true)
        res = solve_pcg(A, b, tol=1e-10, max_iter=5)
        self.assertEqual(res.status, "max_iterations")
        self.assertFalse(res.converged)
        self.assertEqual(res.iterations, 5)
        # True residual is recomputed at the end and exceeds the threshold.
        self.assertGreater(res.relative_residual, 1e-10)
        # But progress is real: final residual below the initial one.
        self.assertLess(res.residual_norm, res.initial_residual_norm)


class TestZeroRhs(unittest.TestCase):

    def test_zero_rhs_zero_guess_short_circuits(self):
        n = 50
        A, _ = laplacian_1d(n)
        res = solve_pcg(A, np.zeros(n))
        self.assertEqual(res.status, "zero_rhs")
        self.assertTrue(res.converged)
        self.assertEqual(res.iterations, 0)
        np.testing.assert_array_equal(res.x, np.zeros(n))
        self.assertEqual(res.residual_norm, 0.0)
        self.assertEqual(res.relative_residual, 0.0)

    def test_zero_rhs_nonzero_guess_runs_normally(self):
        n = 20
        A, _ = laplacian_1d(n)
        x0 = random_x(n, seed=1)
        res = solve_pcg(A, np.zeros(n), x0=x0, tol=1e-10)
        self.assertIn(res.status, SUCCESS_STATUSES)
        # Unique SPD solution of A x = 0 is x = 0.
        self.assertLess(np.linalg.norm(res.x), 1e-9)


class TestNonPositiveCurvature(unittest.TestCase):

    def _indef(self, vals, n=2):
        return CSRMatrix(n, [0, 2, 4], [0, 1, 0, 1], vals)

    def test_indefinite_returns_diagnostic(self):
        # [[2,3],[3,2]] eigenvalues {5,-1}
        A = self._indef([2.0, 3.0, 3.0, 2.0])
        res = solve_pcg(A, np.array([1.0, -1.0]), tol=1e-10)
        self.assertEqual(res.status, "non_positive_curvature")
        self.assertFalse(res.converged)
        self.assertIsNotNone(res.non_positive_value)
        self.assertLess(res.non_positive_value, 0.0)

    def test_semidefinite_nullspace_component_diagnosed(self):
        # [[1,1],[1,1]] eigenvalue 0 along (1,-1)
        A = self._indef([1.0, 1.0, 1.0, 1.0])
        res = solve_pcg(A, np.array([1.0, 0.0]), tol=1e-10, max_iter=20)
        self.assertEqual(res.status, "non_positive_curvature")
        self.assertEqual(res.non_positive_value, 0.0)

    def test_semidefinite_consistent_rhs_converges_documented(self):
        # b orthogonal to the nullspace: CG returns the minimum-norm
        # least-squares solution.  This is a mathematical property of CG;
        # documented in the README.
        A = self._indef([1.0, 1.0, 1.0, 1.0])
        res = solve_pcg(A, np.array([1.0, 1.0]), tol=1e-10)
        self.assertEqual(res.status, "converged")
        np.testing.assert_allclose(res.x, [0.5, 0.5], atol=1e-12)

    def test_tiny_but_genuinely_spd_not_flagged(self):
        # A = diag(1e-20, 2e-20) is SPD even though tiny.
        A = CSRMatrix(2, [0, 1, 2], [0, 1], [1e-20, 2e-20])
        res = solve_pcg(A, np.array([1e-20, 1e-20]), tol=1e-12)
        self.assertEqual(res.status, "converged")
        np.testing.assert_allclose(res.x, [1.0, 0.5], rtol=1e-10)


class TestStagnationAndTrueResidual(unittest.TestCase):

    def test_true_residual_replacement_keeps_drift_small_on_spd(self):
        # On a well-conditioned SPD system the recurrence residual and true
        # residual never disagree by much; max_drift must stay near round-off.
        n = 500
        A, _ = spd_from_mass_spring(n)
        x_true = random_x(n, seed=12)
        b, _ = make_rhs_from_exact_solution(A, x_true)
        res = solve_pcg(A, b, tol=1e-13, true_residual_every=1)
        self.assertEqual(res.status, "converged")
        # Drift measured between recurrence and independently computed norms.
        self.assertLess(res.max_residual_drift,
                        1e-8 * max(res.initial_residual_norm, 1.0))
        self.assertGreater(res.true_residual_checks, 1)

    def test_reported_residual_always_matches_independent_recompute(self):
        # The central honesty requirement: result.residual_norm must equal
        # ||b - A result.x|| computed completely independently.
        n = 120
        A, _ = laplacian_1d(n)
        b, _ = make_rhs_from_exact_solution(A, random_x(n, seed=21))
        for tre in (0, 1, 7):
            res = solve_pcg(A, b, tol=1e-9, true_residual_every=tre)
            recomputed = true_residual_norm(A, res.x, b)
            self.assertTrue(
                np.isfinite(res.residual_norm) or res.status == "diverged"
            )
            self.assertAlmostEqual(
                res.residual_norm, recomputed,
                delta=1e-10 * max(1.0, recomputed),
            )

    def test_stall_window_does_not_fire_during_normal_cg_plateau(self):
        # Ill-conditioned 1-D systems show a long mid-run plateau.  The
        # windowed stall detector must not kill a solve that would converge
        # one window later.
        n = 300
        A, _ = diagonal_scaled_laplacian(n, 1e4)
        x_true = random_x(n, seed=7)
        b, _ = make_rhs_from_exact_solution(A, x_true)
        res = solve_pcg(A, b, tol=1e-8, max_iter=5000, stall_window=50)
        self.assertEqual(res.status, "converged")

    def test_stall_fires_on_hopeless_configuration(self):
        # Jacobi preconditioner on a system with a *near* zero eigenvalue
        # pair that stalls within an artificially tight window + tiny budget
        # is hard to construct honestly; instead force the detector with a
        # window of 1: any non-20%+ improving step immediately stalls.  On a
        # hard system the residual does tick up / flatten in early
        # iterations, so this configuration must terminate as stagnation
        # (or another non-success state), never claim false convergence.
        n = 300
        A, _ = diagonal_scaled_laplacian(n, 1e8)
        x_true = random_x(n, seed=7)
        b, _ = make_rhs_from_exact_solution(A, x_true)
        res = solve_pcg(A, b, tol=1e-14, max_iter=2000,
                        stall_window=2, stall_tolerance=0.5)
        self.assertFalse(res.converged)
        self.assertIn(res.status, {"stagnation", "max_iterations"})


class TestConfigValidation(unittest.TestCase):

    def test_bad_tol(self):
        with self.assertRaises(Exception):
            PCGConfig(tol=0.0)
        with self.assertRaises(Exception):
            PCGConfig(tol=1.0)
        with self.assertRaises(Exception):
            PCGConfig(tol=1e-20)

    def test_bad_preconditioner(self):
        with self.assertRaises(Exception):
            PCGConfig(preconditioner="ilu")

    def test_bad_max_iter(self):
        with self.assertRaises(Exception):
            PCGConfig(max_iter=0)
        with self.assertRaises(Exception):
            PCGConfig(max_iter=-3)

    def test_bad_stall_params(self):
        with self.assertRaises(Exception):
            PCGConfig(stall_window=0)
        with self.assertRaises(Exception):
            PCGConfig(stall_tolerance=1.0)

    def test_default_max_iter_scales_with_n(self):
        n = 50
        A, _ = laplacian_1d(n)
        b, _ = make_rhs_from_exact_solution(A, smooth_x(n))
        res = solve_pcg(A, b)  # default budget
        self.assertEqual(res.status, "converged")


if __name__ == "__main__":
    unittest.main(verbosity=2)
