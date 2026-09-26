"""End-to-end tests for point-to-point 2D ICP on synthetic known transforms."""

import numpy as np
import pytest

from icp2d import ICPConfig, estimate_pose, make_scan_pair, pose_error
from icp2d.synthetic import rectangle_room_scan, straight_wall_scan

GROUND_TRUTH = np.array([0.8, -0.5, 0.15])
ROOM = rectangle_room_scan(spacing=0.05)
WALL = straight_wall_scan(length=20.0, spacing=0.05)


def test_recovers_known_transform_exact_generic_cloud():
    # A 2D-spread generic cloud has no structural corners: point-to-point ICP
    # recovers the exact transform up to floating-point precision.
    rng = np.random.default_rng(0)
    cloud = rng.uniform(-6.0, 6.0, (300, 2))
    scenario = make_scan_pair(cloud, GROUND_TRUTH, seed=0)
    result = estimate_pose(scenario.source, scenario.target, np.zeros(3))
    assert result.converged
    np.testing.assert_allclose(result.pose, GROUND_TRUTH, atol=1e-8)
    assert result.final_rmse < 1e-6


def test_recovers_known_transform_with_noise_outliers_and_partial_overlap():
    scenario = make_scan_pair(
        ROOM,
        GROUND_TRUTH,
        noise_sigma=0.01,
        outlier_count=180,
        overlap_ratio=0.7,
        seed=2,
    )
    result = estimate_pose(scenario.source, scenario.target, np.zeros(3))
    assert result.converged
    error = pose_error(result.pose, GROUND_TRUTH)
    assert error[0] < 0.01
    assert error[1] < 0.01
    assert error[2] < np.deg2rad(1.0)
    # Outliers are rejected: inliers ~= overlapping wall points (325 of 505),
    # never the full cloud plus outliers.
    assert result.num_inliers < result.num_source_points
    assert result.num_inliers > 250


def test_trimmed_strategy_also_recovers_under_partial_overlap():
    scenario = make_scan_pair(
        ROOM,
        GROUND_TRUTH,
        noise_sigma=0.01,
        outlier_count=180,
        overlap_ratio=0.7,
        seed=2,
    )
    config = ICPConfig(rejection_strategy="trimmed", trim_ratio=0.75)
    result = estimate_pose(scenario.source, scenario.target, np.zeros(3), config)
    assert result.converged
    error = pose_error(result.pose, GROUND_TRUTH)
    assert np.linalg.norm(error[:2]) < 0.02
    assert error[2] < np.deg2rad(1.0)


def test_residual_history_is_recorded_every_iteration():
    scenario = make_scan_pair(ROOM, GROUND_TRUTH, noise_sigma=0.01, seed=5)
    result = estimate_pose(scenario.source, scenario.target, np.zeros(3))
    assert len(result.residual_history) == result.iterations
    assert len(result.history) == result.iterations
    assert result.iterations >= 3
    # Overall convergence: the residual drops by more than an order of
    # magnitude and ends at the noise floor.
    assert result.residual_history[0] > 0.1
    assert result.residual_history[-1] == pytest.approx(result.final_rmse)
    assert result.final_rmse < 0.03


def test_step_and_residual_drop_below_tolerance_when_converged():
    scenario = make_scan_pair(ROOM, GROUND_TRUTH, noise_sigma=0.01, seed=5)
    config = ICPConfig()
    result = estimate_pose(scenario.source, scenario.target, np.zeros(3), config)
    assert result.status == "converged"
    last = result.history[-1]
    assert last.step_translation < config.tolerance_translation
    assert last.step_rotation < config.tolerance_rotation


def test_max_iterations_status_when_budget_exhausted():
    scenario = make_scan_pair(ROOM, GROUND_TRUTH, noise_sigma=0.01, seed=5)
    config = ICPConfig(max_iterations=3)
    result = estimate_pose(scenario.source, scenario.target, np.zeros(3), config)
    assert result.status == "max_iterations_reached"
    assert not result.converged
    assert result.iterations == 3


def test_far_initial_guess_is_rejected_as_insufficient_inliers():
    scenario = make_scan_pair(ROOM, GROUND_TRUTH, seed=1)
    result = estimate_pose(
        scenario.source, scenario.target, np.array([10.0, 10.0, 0.0])
    )
    assert result.status == "insufficient_inliers"
    assert result.uncertain
    assert result.num_inliers < ICPConfig().min_inliers


def test_room_geometry_is_not_flagged_degenerate():
    scenario = make_scan_pair(
        ROOM, GROUND_TRUTH, noise_sigma=0.01, outlier_count=100, overlap_ratio=0.7, seed=2
    )
    result = estimate_pose(scenario.source, scenario.target, np.zeros(3))
    assert not result.degeneracy.degenerate
    assert result.degeneracy.flat_translation_direction is None
    assert result.degeneracy.translation_flatness_ratio > 0.2


def test_straight_wall_degeneracy_is_reported_uncertain():
    ground_truth = np.array([1.0, 0.3, 0.0])
    scenario = make_scan_pair(WALL, ground_truth, noise_sigma=0.005, seed=3)
    result = estimate_pose(scenario.source, scenario.target, np.zeros(3))
    assert result.converged
    assert result.uncertain
    report = result.degeneracy
    assert report.degenerate
    # Flat direction is along the wall (the x axis).
    direction = np.array(report.flat_translation_direction)
    assert abs(direction[0]) > 0.95
    assert abs(direction[1]) < 0.05
    assert report.translation_flatness_ratio < 0.05
    # The wall-normal (y) and rotation are still observable:
    np.testing.assert_allclose(result.pose[1], ground_truth[1], atol=0.01)
    np.testing.assert_allclose(result.pose[2], ground_truth[2], atol=0.01)
    # ... while along-wall translation is unobservable (stays at the guess).
    assert abs(result.pose[0] - ground_truth[0]) > 0.5


def test_straight_wall_with_overlap_and_noise_is_uncertain():
    ground_truth = np.array([0.6, -0.4, 0.02])
    scenario = make_scan_pair(
        WALL, ground_truth, noise_sigma=0.02, outlier_count=150,
        overlap_ratio=0.5, seed=10
    )
    result = estimate_pose(scenario.source, scenario.target, np.zeros(3))
    assert result.uncertain
    assert result.degeneracy.degenerate


def test_square_room_symmetry_traps_wrong_initial_guess_in_local_optimum():
    square = rectangle_room_scan(width=10.0, height=10.0, spacing=0.05)
    scenario = make_scan_pair(square, np.zeros(3), noise_sigma=0.005, seed=4)
    # A quarter-turn initial guess: the square is invariant under it.
    result = estimate_pose(
        scenario.source, scenario.target, np.array([0.0, 0.0, np.pi / 2])
    )
    assert result.converged  # ICP reports convergence ...
    assert result.final_rmse < 0.02  # ... at a low residual ...
    # ... but the pose is wrong: this is the documented local optimum.
    assert abs(pose_error(result.pose, np.zeros(3))[2] - np.pi / 2) < 0.05


def test_covariance_positive_semidefinite_on_well_conditioned_problem():
    scenario = make_scan_pair(ROOM, GROUND_TRUTH, noise_sigma=0.01, seed=5)
    result = estimate_pose(scenario.source, scenario.target, np.zeros(3))
    eigenvalues = np.linalg.eigvalsh(result.covariance)
    assert np.all(eigenvalues >= -1e-12)
    assert np.all(np.isfinite(result.covariance))


def test_input_validation():
    with pytest.raises(ValueError):
        estimate_pose(np.zeros((2, 2)), np.zeros((5, 2)))
    with pytest.raises(ValueError):
        estimate_pose(np.zeros((5, 3)), np.zeros((5, 3)))
