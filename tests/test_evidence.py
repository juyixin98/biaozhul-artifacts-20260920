"""Evidence-layer tests: residuals, reconstruction, Vieta, clusters."""

import numpy as np

from app.domain import NormalizedPolynomial
from app.evidence import (
    build_evidence,
    detect_clusters,
    reconstruction_error,
    root_residuals,
    vieta_deviation,
)
from app.kernel import aberth_refine, companion_roots
from tests.conftest import ref_roots


def make_poly(coeffs) -> NormalizedPolynomial:
    arr = np.array(coeffs, dtype=np.complex128)
    return NormalizedPolynomial(coeffs=arr, degree=len(arr) - 1,
                                leading_dropped=0, scale=float(np.max(np.abs(arr))))


class TestResidualsAndReconstruction:
    def test_well_conditioned_case(self, reference):
        case = reference["cases"]["quartic_complex"]
        poly = make_poly(case["coefficients"])
        roots = companion_roots(poly)
        _, res_rel = root_residuals(poly, roots)
        assert np.max(res_rel) < 1e-12
        assert reconstruction_error(poly, roots) < 1e-12

    def test_vieta_via_newton_identities(self, reference):
        case = reference["cases"]["wilkinson_8"]
        poly = make_poly(case["coefficients"])
        roots = companion_roots(poly)
        # Vieta deviation must stay tiny even though individual roots of this
        # polynomial are ill-conditioned — symmetric sums are stable.
        assert vieta_deviation(poly, roots) < 1e-8

    def test_reconstruction_detects_wrong_roots(self, reference):
        case = reference["cases"]["cubic_123"]
        poly = make_poly(case["coefficients"])
        wrong = np.array([1.0, 2.0, 4.0], dtype=np.complex128)  # 3 -> 4
        assert reconstruction_error(poly, wrong) > 0.1


class TestClusters:
    def test_near_multiple_roots_clustered(self, reference):
        # (z-1)^4 (z-2): the four roots near 1 must form ONE cluster of
        # size 4; the root near 2 stays isolated.
        case = reference["cases"]["near_multiple"]
        poly = make_poly(case["coefficients"])
        roots = aberth_refine(poly, companion_roots(poly),
                              max_iter=200, conv_tol=1e-12).roots
        clusters = detect_clusters(roots, cluster_tol=1e-3)
        assert len(clusters) == 1
        assert clusters[0].diameter > 0.0
        assert len(clusters[0].member_indices) == 4

    def test_clustered_roots_not_validated_by_residuals(self, reference):
        # The core anti-"looks correct" test: for (z-1)^4(z-2), computed
        # roots sit ~eps**(1/4) ~ 1e-4 away from 1 while residuals stay
        # ~1e-16. The evidence layer must refuse a residual-based accuracy
        # claim and report the cluster diameter as the bound.
        case = reference["cases"]["near_multiple"]
        poly = make_poly(case["coefficients"])
        roots = aberth_refine(poly, companion_roots(poly),
                              max_iter=200, conv_tol=1e-12).roots
        ev, _, res_rel, error_bound = build_evidence(poly, roots, cluster_tol=1e-3)

        assert ev.max_residual_rel < 1e-10          # residuals look great...
        clustered = set(ev.clusters[0].member_indices)
        true = ref_roots(case)
        for i in clustered:
            fwd = abs(roots[i] - 1.0)                # ...but forward error is large
            assert fwd > 1e-6
            # The reported bound must be the cluster diameter, not the
            # (deceptively small) residual-based estimate.
            assert error_bound[i] == ev.clusters[0].diameter
            assert error_bound[i] >= fwd / 10        # honest order of magnitude
        assert "NOT" in ev.accuracy_note             # residual claim refused

    def test_isolated_roots_have_residual_based_bounds(self, reference):
        case = reference["cases"]["quartic_complex"]
        poly = make_poly(case["coefficients"])
        roots = companion_roots(poly)
        ev, _, _, error_bound = build_evidence(poly, roots, cluster_tol=1e-6)
        assert ev.clusters == []
        assert np.all(error_bound < 1e-10)
