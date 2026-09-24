"""Monte Carlo validation of the first-order covariance propagation.

These tests genuinely sample SE(3), compose the perturbed chain/loop, and
compare the empirical covariance to the analytic propagation. They assert:

* small-angle / short-chain regimes agree tightly (< 3% Frobenius),
* long chains at small noise remain accurate (errors do not blow up),
* large-angle perturbations visibly degrade the approximation,
* the conservative_rho bound PSD-dominates the empirical covariance,
* explicit cross blocks match the empirical correlated covariance,
* missing covariance is refused rather than fabricated.
"""

import numpy as np
import pytest

from app.montecarlo import run_monte_carlo
from app.se3 import exp_se3
from tests.factories import edge

N = 20000


def make_chain(n, s_rot, s_trans=2e-3, seed=10):
    edges = []
    frames = [f"F{i}" for i in range(n + 1)]
    rng = np.random.default_rng(seed)
    for i in range(n):
        R = exp_se3(np.r_[np.zeros(3), rng.standard_normal(3) * 0.2])[:3, :3]
        t = rng.standard_normal(3) * 0.3
        edges.append(
            edge(
                f"F{i+1}",
                f"F{i}",
                translation=t,
                rotation=R,
                trans_std=s_trans,
                rot_std=s_rot,
                edge_id=f"e{i}",
            )
        )
    return edges, frames


@pytest.mark.parametrize("convention", ["right", "left"])
class TestSmallAngle:
    def test_short_chain_matches_monte_carlo(self, convention):
        edges, frames = make_chain(2, 3e-3)
        r = run_monte_carlo(
            edges, frames,
            convention=convention,
            correlation_policy="independent",
            n_samples=N, seed=1,
        )
        assert r["status"] == "ok"
        assert r["relative_frobenius_error"] < 0.04
        assert 0.93 < r["fraction_inside_chi2_95_ellipse"] < 0.97

    def test_loop_closure_matches_monte_carlo(self, convention):
        from app.graph import build_steps, chain_transform, log_se3, parse_edges
        from app.se3 import make_T

        edges, _ = make_chain(2, 3e-3, seed=11)
        T0 = make_T(np.array(edges[0].rotation.matrix), np.array(edges[0].translation))
        T1 = make_T(np.array(edges[1].rotation.matrix), np.array(edges[1].translation))
        # e0 (parent F1, child F0) traversed F0->F1 uses T0 (forward);
        # e1 traversed F1->F2 uses T1 (forward). Walk product (later steps
        # leftmost) is T1 T0, so the forward F2->F0 closing edge must be
        # Tc = (T1 T0)^{-1}.
        from app.se3 import invert
        Tclose = invert(T1 @ T0)
        edges.append(
            edge(
                "F0",
                "F2",
                translation=Tclose[:3, 3],
                rotation=Tclose[:3, :3],
                trans_std=2e-3,
                rot_std=3e-3,
                edge_id="eclose",
            )
        )
        loop_frames = ["F0", "F1", "F2", "F0"]
        # sanity: nominal loop must close before adding noise
        p = parse_edges(edges, "right")
        steps, err = build_steps(p, loop_frames)
        assert err is None
        assert np.linalg.norm(log_se3(chain_transform(steps))) < 1e-9
        r = run_monte_carlo(
            edges, loop_frames,
            convention=convention,
            correlation_policy="independent",
            n_samples=N, seed=2, loop=True,
        )
        assert r["relative_frobenius_error"] < 0.04
        assert 0.93 < r["fraction_inside_chi2_95_ellipse"] < 0.97

    def test_reverse_traversal_matches(self, convention):
        edges, frames = make_chain(2, 3e-3)
        r = run_monte_carlo(
            edges, list(reversed(frames)),
            convention=convention,
            correlation_policy="independent",
            n_samples=N, seed=4,
        )
        assert r["relative_frobenius_error"] < 0.04
        assert 0.93 < r["fraction_inside_chi2_95_ellipse"] < 0.97


class TestLongChain:
    def test_long_chain_small_noise_remains_accurate(self):
        edges, frames = make_chain(20, 2e-3)
        r = run_monte_carlo(
            edges, frames,
            convention="right",
            correlation_policy="independent",
            n_samples=N, seed=11,
        )
        assert r["n_edges"] == 20
        assert r["relative_frobenius_error"] < 0.05
        assert 0.92 < r["fraction_inside_chi2_95_ellipse"] < 0.98


class TestLargeAngleDegradation:
    def test_large_perturbation_degrades_first_order(self):
        # 0.2 rad per edge is no longer "small"; linearisation must visibly slip
        small_edges, sf = make_chain(3, 3e-3, seed=10)
        big_edges, bf = make_chain(3, 0.2, seed=10)
        rs = run_monte_carlo(
            small_edges, sf, convention="right",
            correlation_policy="independent", n_samples=N, seed=9,
        )
        rb = run_monte_carlo(
            big_edges, bf, convention="right",
            correlation_policy="independent", n_samples=N, seed=9,
        )
        # coverage of the nominal 95% ellipse drops for the large-noise case
        assert rb["fraction_inside_chi2_95_ellipse"] < 0.93
        # and it is no better than the small-noise calibration
        assert rb["fraction_inside_chi2_95_ellipse"] < rs[
            "fraction_inside_chi2_95_ellipse"
        ]


class TestConservativeBound:
    def test_bound_dominates_empirical_covariance(self):
        edges, frames = make_chain(3, 5e-3, seed=22)
        r = run_monte_carlo(
            edges, frames,
            convention="right",
            correlation_policy="conservative_rho",
            rho=0.8,
            n_samples=40000, seed=23,
        )
        A = np.array(r["analytic_covariance"])
        E = np.array(r["empirical_covariance"])
        gap = 0.5 * ((A - E) + (A - E).T)
        # the analytic over-bound must sit above the empirical covariance
        assert np.linalg.eigvalsh(gap).min() >= -1e-6


class TestCrossBlocksMC:
    # Isotropic edges (same std on translation and rotation) so that an
    # isotropic cross block C = 0.5 s^2 I is consistent with the marginals
    # and the joint covariance is PSD.
    def _iso_edges(self):
        return make_chain(2, 5e-3, s_trans=5e-3, seed=33)

    def test_explicit_correlation_matches_empirical(self):
        from app.models import CrossBlockSpec

        edges, frames = self._iso_edges()
        s2 = (5e-3) ** 2
        cb = [CrossBlockSpec(i="e0", j="e1", block=(0.5 * s2 * np.eye(6)).tolist())]
        r = run_monte_carlo(
            edges, frames,
            convention="right",
            correlation_policy="cross_blocks",
            cross_blocks=cb,
            n_samples=60000, seed=34,
        )
        assert r["relative_frobenius_error"] < 0.03
        assert 0.93 < r["fraction_inside_chi2_95_ellipse"] < 0.97

    def test_correlation_left_convention(self):
        from app.models import CrossBlockSpec

        edges, frames = self._iso_edges()
        s2 = (5e-3) ** 2
        cb = [CrossBlockSpec(i="e0", j="e1", block=(0.5 * s2 * np.eye(6)).tolist())]
        r = run_monte_carlo(
            edges, frames,
            convention="left",
            correlation_policy="cross_blocks",
            cross_blocks=cb,
            n_samples=60000, seed=34,
        )
        assert r["relative_frobenius_error"] < 0.03


class TestMissingCovarianceMC:
    def test_missing_covariance_skipped_not_fabricated(self):
        edges, frames = make_chain(2, 3e-3)
        edges[1] = edges[1].model_copy(update={"covariance": None})
        r = run_monte_carlo(
            edges, frames, convention="right",
            correlation_policy="independent", n_samples=100,
        )
        assert r["status"] == "skipped"
        assert r["reason"] == "missing_covariance"
        assert "e1" in r["missing_edges"]
        assert "NOT treated as zero" in r["message"]
