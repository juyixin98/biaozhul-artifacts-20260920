"""Tests for the point-to-point ICP implementation.

Acceptance coverage:
  * synthetic known-transform point sets
  * outliers injected into the source cloud
  * partial overlap between scans
  * straight-line degenerate geometry -> result flagged uncertain
  * wrong initial guess -> converges to a local optimum (documented)
"""

import math

import numpy as np
import pytest

from scan_matching.geometry import SE2Pose, compose, inverse, pose_error
from scan_matching.icp import (
    ICPParams,
    STATUS_CONVERGED,
    STATUS_FAILED,
    icp,
    nearest_neighbors,
    reject_outliers,
)
from scan_matching.synthetic import (
    add_outliers,
    generate_scene,
    generate_scan,
    partial_overlap_scan,
)


def _make_pair(scene_shape="room", source_pose=SE2Pose(0.3, -0.15, 0.08),
               target_pose=SE2Pose(), noise_sigma=0.01, seed=42):
    """Two scans of one scene plus the true relative pose source->target."""
    rng = np.random.default_rng(seed)
    scene = generate_scene(scene_shape)
    source = generate_scan(scene, source_pose, noise_sigma=noise_sigma, rng=rng)
    target = generate_scan(scene, target_pose, noise_sigma=noise_sigma, rng=rng)
    truth = compose(inverse(target_pose), source_pose)
    return source, target, truth


class TestNearestNeighbors:
    def test_finds_closest_point(self):
        target = np.array([[0.0, 0.0], [5.0, 5.0]])
        idx, dist = nearest_neighbors(np.array([[0.1, 0.0], [4.9, 5.1]]), target)
        assert list(idx) == [0, 1]
        assert dist[0] == pytest.approx(0.1)
        assert dist[1] == pytest.approx(math.hypot(0.1, 0.1))

    def test_reject_outliers_distance_gate(self):
        distances = np.array([0.1, 0.2, 5.0, 0.3])
        keep = reject_outliers(distances, max_distance=1.0, trim_ratio=1.0)
        assert list(keep) == [0, 1, 3]

    def test_reject_outliers_trim_ratio_keeps_best(self):
        distances = np.array([0.8, 0.1, 0.7, 0.2, 0.6, 0.3, 0.5, 0.4])
        keep = reject_outliers(distances, max_distance=10.0, trim_ratio=0.5)
        assert list(keep) == [1, 3, 5, 7]


class TestKnownTransform:
    def test_recovers_known_transform(self):
        source, target, truth = _make_pair()
        result = icp(source, target)
        assert result.status == STATUS_CONVERGED
        assert result.converged
        assert not result.degenerate
        err = pose_error(result.pose, truth)
        assert err["translation"] < 0.02
        assert err["rotation"] < 0.01
        # Residual should be near the noise floor and decreasing overall.
        assert result.residual < 0.05
        residuals = [r.residual for r in result.iterations]
        assert residuals[-1] <= residuals[0]

    def test_iteration_records_are_complete(self):
        source, target, _ = _make_pair()
        result = icp(source, target)
        assert len(result.iterations) >= 2
        for record in result.iterations:
            assert record.num_correspondences >= 3
            assert record.residual > 0.0
            assert record.delta_translation >= 0.0

    def test_identity_transform(self):
        rng = np.random.default_rng(1)
        scene = generate_scene("room")
        target = generate_scan(scene, SE2Pose(), noise_sigma=0.005, rng=rng)
        result = icp(target, target)
        assert result.converged
        err = pose_error(result.pose, SE2Pose())
        assert err["translation"] < 1e-3
        assert err["rotation"] < 1e-3


class TestOutliers:
    def test_outliers_in_source_are_rejected(self):
        rng = np.random.default_rng(7)
        source, target, truth = _make_pair()
        source = add_outliers(source, count=40, rng=rng, spread=4.0)
        params = ICPParams(max_correspondence_distance=1.5, trim_ratio=0.9)
        result = icp(source, target, params=params)
        assert result.converged
        err = pose_error(result.pose, truth)
        assert err["translation"] < 0.05
        assert err["rotation"] < 0.02

    def test_no_correspondences_fails_cleanly(self):
        rng = np.random.default_rng(3)
        scene = generate_scene("room")
        target = generate_scan(scene, SE2Pose(), rng=rng)
        far_away = np.array([[100.0, 100.0], [101.0, 100.0],
                             [100.0, 101.0], [101.0, 101.0]])
        result = icp(far_away, target,
                     params=ICPParams(max_correspondence_distance=1.0))
        assert result.status == STATUS_FAILED
        assert not result.converged
        assert "correspondences" in result.message


class TestPartialOverlap:
    def test_partial_overlap_scans(self):
        rng = np.random.default_rng(11)
        scene = generate_scene("room")
        target_pose = SE2Pose(0.0, 0.0, 0.0)
        source_pose = SE2Pose(0.8, 0.4, 0.15)
        target = generate_scan(scene, target_pose, max_range=6.0,
                               noise_sigma=0.01, rng=rng)
        source = partial_overlap_scan(scene, source_pose, keep_fraction=0.7,
                                      max_range=6.0, noise_sigma=0.01, rng=rng)
        truth = compose(inverse(target_pose), source_pose)
        params = ICPParams(max_correspondence_distance=1.5, trim_ratio=0.85)
        result = icp(source, target, params=params)
        assert result.converged
        err = pose_error(result.pose, truth)
        assert err["translation"] < 0.08
        assert err["rotation"] < 0.03


class TestDegeneracy:
    def test_straight_line_is_flagged_degenerate(self):
        source, target, _ = _make_pair(
            scene_shape="line",
            source_pose=SE2Pose(0.4, 0.2, 0.0),
            noise_sigma=0.005, seed=7)
        result = icp(source, target,
                     params=ICPParams(max_correspondence_distance=1.0))
        assert result.degenerate
        assert result.degenerate_direction is not None
        # The unconstrained direction must lie along the wall (x axis).
        dx, dy = result.degenerate_direction
        assert abs(dx) > 0.99
        assert abs(dy) < 0.1
        assert "uncertain" in result.message

    def test_room_scene_is_not_degenerate(self):
        source, target, _ = _make_pair()
        result = icp(source, target)
        assert not result.degenerate
        assert result.degenerate_direction is None


class TestLocalOptimum:
    def test_wrong_initial_guess_converges_to_local_optimum(self):
        """A 180-degree initial error lands in a local optimum: ICP reports
        convergence but the pose is far from ground truth. This documents
        that ICP is a local method and needs a reasonable initial guess."""
        source, target, truth = _make_pair()
        bad_initial = SE2Pose(x=0.0, y=0.0, theta=math.pi)
        result = icp(source, target, initial_pose=bad_initial,
                     params=ICPParams(max_correspondence_distance=3.0))
        err = pose_error(result.pose, truth)
        # Converged (or stalled) but clearly wrong: local optimum.
        assert err["translation"] > 0.5 or err["rotation"] > 0.5

    def test_good_initial_guess_avoids_local_optimum(self):
        source, target, truth = _make_pair()
        good_initial = SE2Pose(x=truth.x - 0.1, y=truth.y + 0.1,
                               theta=truth.theta - 0.05)
        result = icp(source, target, initial_pose=good_initial)
        assert result.converged
        err = pose_error(result.pose, truth)
        assert err["translation"] < 0.02
        assert err["rotation"] < 0.01


class TestInputValidation:
    def test_rejects_malformed_points(self):
        with pytest.raises(ValueError, match="source"):
            icp(np.zeros((4, 3)), np.zeros((5, 2)))
        with pytest.raises(ValueError, match="non-finite"):
            icp(np.array([[np.nan, 0.0]] * 4), np.zeros((5, 2)))
        with pytest.raises(ValueError, match="at least"):
            icp(np.zeros((2, 2)), np.zeros((5, 2)))

    def test_rejects_bad_params(self):
        with pytest.raises(ValueError):
            ICPParams(trim_ratio=0.0)
        with pytest.raises(ValueError):
            ICPParams(max_iterations=0)
        with pytest.raises(ValueError, match="unknown"):
            ICPParams.from_dict({"not_a_param": 1})
