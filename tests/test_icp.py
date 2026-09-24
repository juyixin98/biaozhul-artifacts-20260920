"""Tests for the SVD rigid estimator and the ICP loop."""

import numpy as np
import pytest

from app.icp_core import (
    ICPStatus,
    estimate_rigid_transform,
    icp,
    rotation_error_deg,
    translation_error,
    validate_rotation,
)
from app.synthetic import make_scene, rotation_about_axis


def test_estimate_rigid_recovers_known_transform():
    rng = np.random.default_rng(1)
    pts = rng.uniform(-1, 1, size=(60, 3))
    R_true = rotation_about_axis([0.3, -0.7, 0.5], 34.0)
    t_true = np.array([1.2, -0.4, 0.9])
    dst = (R_true @ pts.T).T + t_true

    R, t, deg, reflected = estimate_rigid_transform(pts, dst)
    assert reflected is False
    assert deg.degenerate is False
    assert np.allclose(R, R_true, atol=1e-9)
    assert np.allclose(t, t_true, atol=1e-9)
    assert abs(np.linalg.det(R) - 1.0) < 1e-10


def test_estimate_rigid_rejects_reflection():
    # Mirror configuration: a planar set matched to its mirror image has a
    # better reflection fit; the estimator must still return SO(3).
    src = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0],
                    [0.0, 1.0, 0.0], [1.0, 1.0, 0.0]])
    dst = src.copy()
    dst[:, 0] *= -1.0  # reflection through the yz plane
    R, t, _deg, reflected = estimate_rigid_transform(src, dst)
    assert reflected is True
    assert abs(np.linalg.det(R) - 1.0) < 1e-10
    assert np.allclose(R.T @ R, np.eye(3), atol=1e-10)
    assert np.allclose(t, 0.0, atol=1e-10)


def test_estimate_rigid_collinear_degeneracy():
    src = np.zeros((20, 3))
    src[:, 0] = np.linspace(-1, 1, 20)
    dst = src.copy()  # identity is exact
    R, t, deg, reflected = estimate_rigid_transform(src, dst)
    assert deg.degenerate is True
    assert deg.kind == "collinear"
    assert deg.rank == 1
    assert np.allclose(R, np.eye(3), atol=1e-8)
    assert reflected is False


def test_estimate_rigid_requires_three_pairs():
    with pytest.raises(ValueError, match="at least 3"):
        estimate_rigid_transform(np.zeros((2, 3)), np.zeros((2, 3)))


def test_validate_rotation_rejects_non_orthogonal():
    with pytest.raises(ValueError, match="orthogonal"):
        validate_rotation(np.eye(3) * 2.0)
    with pytest.raises(ValueError, match="det"):
        validate_rotation(np.diag([1.0, 1.0, -1.0]))


def test_icp_full_overlap_noise_accuracy():
    scene = make_scene(n_points=200, kind="volume", angle_deg=20.0,
                       translation=0.5, noise_std=0.01, seed=42)
    res = icp(scene.source, scene.target,
              max_iterations=100, robust_quantile=1.0)
    assert res.status is ICPStatus.CONVERGED
    assert res.rmse == pytest.approx(0.01, abs=0.01)
    assert rotation_error_deg(res.R, scene.R_true) < 1.0
    assert translation_error(res.t, scene.t_true) < 0.02
    assert res.inlier_count == 200
    assert res.degeneracy.degenerate is False


def test_icp_perfect_transform_single_step():
    scene = make_scene(n_points=100, kind="volume", angle_deg=10.0,
                       translation=0.2, noise_std=0.0, seed=7)
    res = icp(scene.source, scene.target,
              scene.R_true, scene.t_true, robust_quantile=1.0)
    assert res.status is ICPStatus.CONVERGED
    assert res.rmse < 1e-9
    assert rotation_error_deg(res.R, scene.R_true) < 1e-6


def _collinear_cloud(n=60, seed=3):
    rng = np.random.default_rng(seed)
    source = np.zeros((n, 3))
    source[:, 0] = rng.uniform(-1, 1, n)
    return source


def test_icp_collinear_true_correspondences_exact_but_degenerate():
    # With known correspondences the estimator is exact AND reports that the
    # rotation about the line is not observable from geometry alone.
    source = _collinear_cloud()
    R_true = rotation_about_axis([0, 0, 1], 30.0)
    t_true = np.array([0.3, 0.2, 0.0])
    target = (R_true @ source.T).T + t_true
    R, t, deg, _ = estimate_rigid_transform(source, target)
    assert deg.degenerate and deg.kind == "collinear"
    assert np.allclose(R, R_true, atol=1e-9)
    assert np.allclose(t, t_true, atol=1e-9)


def test_icp_collinear_reports_degeneracy_with_good_initial_guess():
    # With an initial guess already close to truth, nearest-neighbour pairing
    # is correct; ICP then aligns the line exactly, while still reporting
    # collinear degeneracy because rotation about the line is unobservable.
    source = _collinear_cloud()
    R_true = rotation_about_axis([0, 0, 1], 30.0)
    t_true = np.array([0.3, 0.2, 0.0])
    target = (R_true @ source.T).T + t_true

    res = icp(source, target, R_true, t_true,
              max_iterations=100, robust_quantile=1.0)
    assert res.status is ICPStatus.CONVERGED
    assert res.degeneracy.degenerate is True
    assert res.degeneracy.kind == "collinear"
    assert any("collinear" in w for w in res.warnings)
    assert res.rmse < 1e-8
    assert translation_error(res.t, t_true) < 1e-6


def test_icp_collinear_poor_initial_guess_is_honest_local_minimum():
    # A collinear cloud registered from a distant guess: nearest-neighbour
    # associations on the line are ambiguous, so ICP settles on a wrong
    # local minimum. The backend must surface this, never claim success.
    source = _collinear_cloud()
    R_true = rotation_about_axis([0, 0, 1], 30.0)
    t_true = np.array([0.3, 0.2, 0.0])
    target = (R_true @ source.T).T + t_true

    res = icp(source, target, max_iterations=200, robust_quantile=1.0)
    assert res.degeneracy.degenerate is True
    rot_err = rotation_error_deg(res.R, R_true)
    if rot_err > 1.0:
        # Trapped in a wrong local minimum (expected for collinear data).
        assert res.rmse is not None and res.rmse > 1e-3
    # Either way: no global-optimality claim is allowed.
    assert all("global optimum" not in
               w.replace("NOT verified as the global optimum", "")
               for w in res.warnings)


def test_icp_planar_rotation_recovered_but_flagged():
    # Flat square cloud: rank-2 geometry. Rotation in the plane about the
    # normal is recoverable here, but the weak direction must be reported.
    rng = np.random.default_rng(0)
    n = 120
    source = np.zeros((n, 3))
    source[:, :2] = rng.uniform(-1, 1, size=(n, 2))
    source[:, 0] *= 0.7  # anisotropic in-plane covariance
    R_true = rotation_about_axis([0, 0, 1], 25.0)
    t_true = np.array([0.2, -0.1, 0.3])
    target = (R_true @ source.T).T + t_true
    target += rng.normal(scale=1e-6, size=target.shape)

    res = icp(source, target, max_iterations=100, robust_quantile=1.0)
    assert res.status is ICPStatus.CONVERGED
    assert res.degeneracy.degenerate is True
    assert res.degeneracy.kind == "planar"
    assert rotation_error_deg(res.R, R_true) < 1e-3
    assert translation_error(res.t, t_true) < 1e-4


def test_icp_partial_overlap_with_clutter():
    scene = make_scene(n_points=200, kind="volume", angle_deg=15.0,
                       translation=0.3, noise_std=0.01, overlap=0.6,
                       n_clutter=80, seed=11)
    res = icp(scene.source, scene.target,
              max_iterations=100,
              robust_quantile=0.65,
              max_correspondence_distance=1.5)
    assert res.status is ICPStatus.CONVERGED
    assert res.inlier_count <= int(0.65 * 200)
    assert res.inlier_count >= 100
    assert rotation_error_deg(res.R, scene.R_true) < 2.0
    assert translation_error(res.t, scene.t_true) < 0.05
    assert res.rmse < 0.05


def test_icp_untrimmed_overlap_warns_or_fails_honestly():
    # With no trimming at all, extra clutter/overlap should either raise the
    # residual (local-minimum warning) or be clearly visible in the numbers.
    scene = make_scene(n_points=200, kind="volume", angle_deg=15.0,
                       translation=0.3, noise_std=0.01, overlap=0.6,
                       n_clutter=80, seed=11)
    res = icp(scene.source, scene.target,
              max_iterations=100, robust_quantile=1.0)
    # Never falsely reported as a clean global success.
    assert res.status in (ICPStatus.CONVERGED, ICPStatus.MAX_ITERATIONS)
    if res.status is ICPStatus.CONVERGED:
        assert any("local minimum" in w or "global optimality" in w
                   for w in res.warnings)


def test_icp_bad_initial_guess_not_claimed_global():
    scene = make_scene(n_points=150, kind="clusters", angle_deg=25.0,
                       translation=0.2, noise_std=0.005, seed=5)
    R0 = scene.R_true @ rotation_about_axis([0, 0, 1], 140.0)
    res = icp(scene.source, scene.target, R0, scene.t_true,
              max_iterations=100, robust_quantile=1.0)

    assert res.status in (ICPStatus.CONVERGED, ICPStatus.MAX_ITERATIONS)
    # No field or warning may assert global optimality.
    assert all("global optimum" not in w.replace("NOT verified as the global optimum", "")
                for w in res.warnings)
    err = rotation_error_deg(res.R, scene.R_true)
    if err > 2.0:
        # Trapped in a local minimum: the warning must say so.
        assert any("local minimum" in w for w in res.warnings)
        assert res.rmse is not None and res.rmse > 0.02
    else:
        # Escaped: still only a local-convergence claim is allowed.
        assert any("local" in w for w in res.warnings)


def test_icp_reports_nonconvergence():
    scene = make_scene(n_points=150, kind="volume", angle_deg=60.0,
                       translation=1.0, noise_std=0.01, seed=9)
    res = icp(scene.source, scene.target,
              max_iterations=2, robust_quantile=1.0)
    assert res.status is ICPStatus.MAX_ITERATIONS
    assert any("did not converge" in w for w in res.warnings)


def test_icp_insufficient_pairs_with_distance_cutoff():
    scene = make_scene(n_points=60, kind="volume", angle_deg=30.0,
                       translation=5.0, noise_std=0.0, seed=1)
    res = icp(scene.source, scene.target,
              max_iterations=20, robust_quantile=1.0,
              max_correspondence_distance=0.01, min_inliers=3)
    assert res.status is ICPStatus.INSUFFICIENT_PAIRS
    assert res.inlier_count < 3
    assert any("inlier" in w for w in res.warnings)
    # Returns the (identity) initial guess untouched.
    assert np.allclose(res.R, np.eye(3))
    assert np.allclose(res.t, 0.0)


def test_icp_validates_inputs():
    with pytest.raises(ValueError):
        icp(np.zeros((0, 3)), np.zeros((3, 3)))
    with pytest.raises(ValueError):
        icp(np.zeros((5, 3)), np.zeros((5, 3)), R0=np.eye(3) * 2)
    with pytest.raises(ValueError):
        icp(np.zeros((5, 3)), np.zeros((5, 3)), robust_quantile=1.5)
    with pytest.raises(ValueError):
        icp(np.zeros((5, 3)), np.zeros((5, 3)), min_inliers=2)


def test_icp_rmse_monotonically_decreases():
    scene = make_scene(n_points=120, kind="volume", angle_deg=25.0,
                       translation=0.4, noise_std=0.005, seed=13)
    res = icp(scene.source, scene.target,
              max_iterations=100, robust_quantile=1.0)
    hist = np.asarray(res.history)
    # ICP's correspondence-and-solve structure guarantees non-increasing
    # objective between the first few oscillatory entries once associations
    # settle; check the overall trend from the first to the last.
    assert hist[-1] <= hist[0] + 1e-12
    assert np.all(np.diff(hist) <= 1e-9)


def test_icp_deterministic_given_same_inputs():
    scene = make_scene(n_points=80, kind="volume", angle_deg=18.0,
                       translation=0.3, noise_std=0.01, seed=21)
    r1 = icp(scene.source, scene.target, max_iterations=30, robust_quantile=0.8)
    r2 = icp(scene.source, scene.target, max_iterations=30, robust_quantile=0.8)
    assert np.array_equal(r1.R, r2.R)
    assert np.array_equal(r1.t, r2.t)
    assert r1.history == r2.history
