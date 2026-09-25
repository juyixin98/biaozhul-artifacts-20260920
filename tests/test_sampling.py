import numpy as np
import pytest

from trajvel.parameterize import parameterize
from trajvel.sampling import build_segment_profile, sample_trajectory

# Finite-difference of discrete samples slightly exceeds the analytic bound;
# allow one sample's worth of slack.
FD_TOL = 1e-2


def test_sampled_straight_line_respects_bounds():
    result = parameterize(
        [[0.0, 0.0], [10.0, 0.0]],
        max_velocity=2.0,
        max_acceleration=1.0,
        max_lateral_acceleration=1.0,
    )
    samples = sample_trajectory(result, max_acceleration=1.0, max_velocity=2.0, dt=0.01)
    assert samples["t"][-1] == pytest.approx(result.total_time, abs=1e-9)
    assert np.max(samples["speeds"]) <= 2.0 + FD_TOL
    assert np.max(np.abs(samples["accelerations"])) <= 1.0 + FD_TOL
    # Ends at the final waypoint, starts at the first.
    np.testing.assert_allclose(samples["positions"][0], [0.0, 0.0], atol=1e-9)
    np.testing.assert_allclose(samples["positions"][-1], [10.0, 0.0], atol=1e-6)
    # Start and end at rest.
    assert samples["speeds"][0] == pytest.approx(0.0, abs=1e-9)
    assert samples["speeds"][-1] == pytest.approx(0.0, abs=1e-9)


def test_sampled_speed_matches_finite_difference_of_position():
    result = parameterize(
        [[0.0, 0.0], [4.0, 0.0], [4.0, 3.0]],
        max_velocity=2.0,
        max_acceleration=1.5,
        max_lateral_acceleration=1.0,
        corner_strategy="stop",
    )
    dt = 0.005
    samples = sample_trajectory(result, max_acceleration=1.5, max_velocity=2.0, dt=dt)
    positions = samples["positions"]
    # Arc length advanced between consecutive samples must equal speed*dt
    # (up to the corner, where the direction changes at near-zero speed).
    steps = np.linalg.norm(np.diff(positions, axis=0), axis=1)
    expected = 0.5 * (samples["speeds"][:-1] + samples["speeds"][1:]) * dt
    np.testing.assert_allclose(steps, expected, atol=1e-3)
    assert np.max(samples["speeds"]) <= 2.0 + FD_TOL
    assert np.max(np.abs(samples["accelerations"])) <= 1.5 + FD_TOL


def test_sampled_corner_slowdown_visible():
    result = parameterize(
        [[0.0, 0.0], [4.0, 0.0], [4.0, 3.0]],
        max_velocity=2.0,
        max_acceleration=1.5,
        max_lateral_acceleration=1.0,
        corner_strategy="curvature",
    )
    samples = sample_trajectory(result, max_acceleration=1.5, max_velocity=2.0, dt=0.01)
    corner_time = result.waypoint_times[1]
    index = int(np.argmin(np.abs(samples["t"] - corner_time)))
    assert samples["speeds"][index] == pytest.approx(
        result.node_velocities[1], abs=2e-2
    )


def test_build_segment_profile_zero_length_is_inert():
    profile = build_segment_profile(0.0, 0.0, 0.0, 1.0, 1.0)
    assert profile.duration == 0.0


def test_sample_zero_length_input_stays_finite():
    result = parameterize(
        [[0.0, 0.0], [1.0, 0.0], [1.0, 0.0], [1.0, 1.0]],
        max_velocity=1.0,
        max_acceleration=1.0,
        max_lateral_acceleration=1.0,
        corner_strategy="stop",
    )
    samples = sample_trajectory(result, max_acceleration=1.0, max_velocity=1.0, dt=0.01)
    assert np.all(np.isfinite(samples["positions"]))
    assert np.all(np.isfinite(samples["speeds"]))
    np.testing.assert_allclose(samples["positions"][-1], [1.0, 1.0], atol=1e-6)


def test_sample_rejects_nonpositive_dt():
    result = parameterize(
        [[0.0, 0.0], [1.0, 0.0]],
        max_velocity=1.0,
        max_acceleration=1.0,
        max_lateral_acceleration=1.0,
    )
    with pytest.raises(ValueError):
        sample_trajectory(result, 1.0, 1.0, dt=0.0)
