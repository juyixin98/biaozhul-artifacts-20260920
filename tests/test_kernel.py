"""Kernel tests against the independent mpmath reference fixture."""

import numpy as np
import pytest

from app.domain import NormalizedPolynomial
from app.errors import ComputationFailedError
from app.kernel import aberth_refine, cauchy_initial_guesses, companion_roots
from tests.conftest import match_max_error, ref_roots


def make_poly(coeffs) -> NormalizedPolynomial:
    arr = np.array(coeffs, dtype=np.complex128)
    return NormalizedPolynomial(coeffs=arr, degree=len(arr) - 1,
                                leading_dropped=0, scale=float(np.max(np.abs(arr))))


class TestCompanion:
    def test_cubic_known_roots(self, reference):
        case = reference["cases"]["cubic_123"]
        roots = companion_roots(make_poly(case["coefficients"]))
        assert match_max_error(list(roots), ref_roots(case)) < 1e-12

    def test_quartic_complex_conjugates(self, reference):
        case = reference["cases"]["quartic_complex"]
        roots = companion_roots(make_poly(case["coefficients"]))
        assert match_max_error(list(roots), ref_roots(case)) < 1e-12

    def test_sparse_high_order(self, reference):
        case = reference["cases"]["sparse_20"]
        roots = companion_roots(make_poly(case["coefficients"]))
        assert len(roots) == 20
        assert match_max_error(list(roots), ref_roots(case)) < 1e-12
        # Structural check: all roots on the unit circle.
        np.testing.assert_allclose(np.abs(roots), 1.0, atol=1e-12)

    def test_wilkinson_8_against_mpmath(self, reference):
        # Ill-conditioned: forward error is genuinely large, so the tolerance
        # is loose — the point is to compare against the independent
        # high-precision reference, not to claim float64 magic.
        case = reference["cases"]["wilkinson_8"]
        roots = companion_roots(make_poly(case["coefficients"]))
        assert match_max_error(list(roots), ref_roots(case)) < 1e-4


class TestAberth:
    def test_converges_from_cauchy_guesses(self, reference):
        case = reference["cases"]["quartic_complex"]
        poly = make_poly(case["coefficients"])
        result = aberth_refine(poly, cauchy_initial_guesses(poly),
                               max_iter=200, conv_tol=1e-13)
        assert np.all(result.converged)
        assert match_max_error(list(result.roots), ref_roots(case)) < 1e-10

    def test_polishes_companion_roots(self, reference):
        case = reference["cases"]["wilkinson_8"]
        poly = make_poly(case["coefficients"])
        # conv_tol=1e-11: float64 evaluation of this polynomial near its
        # roots carries ~1e-12 noise, so 1e-13 convergence is unattainable
        # and would (correctly) report unconverged.
        result = aberth_refine(poly, companion_roots(poly),
                               max_iter=100, conv_tol=1e-11)
        assert np.all(result.converged)
        assert match_max_error(list(result.roots), ref_roots(case)) < 1e-6

    def test_iteration_exhaustion_preserves_state(self, reference):
        # max_iter=0: no refinement happens, but the unconverged state must
        # be returned intact — not dropped, not marked converged.
        case = reference["cases"]["cubic_123"]
        poly = make_poly(case["coefficients"])
        initial = cauchy_initial_guesses(poly)
        result = aberth_refine(poly, initial, max_iter=0, conv_tol=1e-13)
        assert result.steps_taken == 0
        assert not np.any(result.converged)
        np.testing.assert_array_equal(result.roots, initial)

    def test_tiny_budget_reports_unconverged(self, reference):
        case = reference["cases"]["sparse_20"]
        poly = make_poly(case["coefficients"])
        result = aberth_refine(poly, cauchy_initial_guesses(poly),
                               max_iter=1, conv_tol=1e-14)
        assert result.steps_taken == 1
        assert not np.all(result.converged)
        assert len(result.roots) == 20  # roots preserved, not dropped

    def test_mismatched_guess_count_fails(self, reference):
        case = reference["cases"]["cubic_123"]
        poly = make_poly(case["coefficients"])
        with pytest.raises(ComputationFailedError):
            aberth_refine(poly, np.array([1.0 + 0.0j]), max_iter=10, conv_tol=1e-12)
