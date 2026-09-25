import math

import numpy as np
import pytest

from trajvel.parameterize import (
    CornerStrategy,
    corner_curvature,
    parameterize,
    segment_duration,
)
from trajvel.geometry import segment_lengths

TOL = 1e-9


def test_straight_line_trapezoidal_total_time():
    # 10 m line, vmax=2, amax=1: accel 2 s over 2 m, cruise 6 m in 3 s,
    # decel 2 s -> total exactly 7 s.
    result = parameterize(
        [[0.0, 0.0], [10.0, 0.0]],
        max_velocity=2.0,
        max_acceleration=1.0,
        max_lateral_acceleration=1.0,
    )
    assert result.total_time == pytest.approx(7.0, abs=1e-12)
    assert result.node_velocities[0] == 0.0
    assert result.node_velocities[-1] == 0.0
    assert np.all(result.node_velocities <= 2.0 + TOL)


def test_straight_line_triangular_profile():
    # 4 m line, vmax=10 (unreachable), amax=1: peak speed sqrt(1*4)=2,
    # total time 2*2/1 = 4 s.
    result = parameterize(
        [[0.0, 0.0], [4.0, 0.0]],
        max_velocity=10.0,
        max_acceleration=1.0,
        max_lateral_acceleration=1.0,
    )
    assert result.total_time == pytest.approx(4.0, abs=1e-12)


def test_sharp_corner_curvature_strategy_limits_speed():
    waypoints = [[0.0, 0.0], [4.0, 0.0], [4.0, 3.0]]  # 90-degree corner
    result = parameterize(
        waypoints,
        max_velocity=2.0,
        max_acceleration=1.5,
        max_lateral_acceleration=1.0,
        corner_strategy="curvature",
    )
    corner_v = result.node_velocities[1]
    kappa = corner_curvature(result.points, segment_lengths(result.points), 1)
    theoretical_cap = math.sqrt(1.0 / kappa)
    assert corner_v == pytest.approx(theoretical_cap, rel=1e-9)
    assert corner_v < 2.0  # corner is slower than the straight-line limit
    assert 1 in result.corner_velocities


def test_sharp_corner_stop_strategy_forces_full_stop():
    result = parameterize(
        [[0.0, 0.0], [4.0, 0.0], [4.0, 3.0]],
        max_velocity=2.0,
        max_acceleration=1.5,
        max_lateral_acceleration=1.0,
        corner_strategy="stop",
    )
    assert result.node_velocities[1] == 0.0


def test_stop_strategy_slower_than_curvature_strategy():
    waypoints = [[0.0, 0.0], [4.0, 0.0], [4.0, 3.0]]
    kwargs = dict(max_velocity=2.0, max_acceleration=1.5, max_lateral_acceleration=1.0)
    curved = parameterize(waypoints, corner_strategy="curvature", **kwargs)
    stopped = parameterize(waypoints, corner_strategy="stop", **kwargs)
    assert stopped.total_time > curved.total_time


def test_zero_length_segments_no_divide_by_zero():
    waypoints = [[0.0, 0.0], [2.0, 0.0], [2.0, 0.0], [2.0, 0.0], [2.0, 3.0]]
    result = parameterize(
        waypoints,
        max_velocity=1.5,
        max_acceleration=1.0,
        max_lateral_acceleration=0.8,
        corner_strategy="stop",
    )
    assert result.removed_duplicate_points == 2
    assert np.all(np.isfinite(result.node_velocities))
    assert np.all(np.isfinite(result.segment_durations))
    assert math.isfinite(result.total_time)
    # Same as the manually deduplicated input.
    reference = parameterize(
        [[0.0, 0.0], [2.0, 0.0], [2.0, 3.0]],
        max_velocity=1.5,
        max_acceleration=1.0,
        max_lateral_acceleration=0.8,
        corner_strategy="stop",
    )
    assert result.total_time == pytest.approx(reference.total_time, rel=1e-12)


def test_all_duplicate_points_do_not_crash():
    result = parameterize(
        [[1.0, 1.0], [1.0, 1.0], [1.0, 1.0]],
        max_velocity=1.0,
        max_acceleration=1.0,
        max_lateral_acceleration=1.0,
    )
    assert result.total_time == pytest.approx(0.0, abs=1e-12)
    assert np.all(np.isfinite(result.node_velocities))


def test_forward_backward_feasibility_invariant():
    rng = np.random.default_rng(42)
    waypoints = rng.uniform(-5.0, 5.0, size=(8, 3))
    amax = 1.2
    result = parameterize(
        waypoints,
        max_velocity=2.0,
        max_acceleration=amax,
        max_lateral_acceleration=0.9,
        corner_strategy="curvature",
    )
    lengths = segment_lengths(result.points)
    v = result.node_velocities
    for i, s in enumerate(lengths):
        assert v[i + 1] ** 2 <= v[i] ** 2 + 2.0 * amax * s + 1e-9
        assert v[i] ** 2 <= v[i + 1] ** 2 + 2.0 * amax * s + 1e-9
    assert np.all(v <= 2.0 + TOL)


def test_start_and_end_velocity_respected():
    result = parameterize(
        [[0.0, 0.0], [10.0, 0.0]],
        max_velocity=2.0,
        max_acceleration=1.0,
        max_lateral_acceleration=1.0,
        start_velocity=1.0,
        end_velocity=0.5,
    )
    assert result.node_velocities[0] == pytest.approx(1.0)
    assert result.node_velocities[-1] == pytest.approx(0.5)
    # accel 1->2 over 1.5 m in 1 s, cruise 6.625 m in 3.3125 s, decel 2->0.5
    # over 1.875 m in 1.5 s -> total 5.8125 s
    assert result.total_time == pytest.approx(5.8125, abs=1e-12)


def test_segment_duration_zero_length_is_zero():
    assert segment_duration(0.0, 0.0, 0.0, 1.0, 1.0) == 0.0


def test_invalid_constraints_rejected():
    with pytest.raises(ValueError):
        parameterize([[0.0], [1.0]], 0.0, 1.0, 1.0)  # zero max_velocity
    with pytest.raises(ValueError):
        parameterize([[0.0], [1.0]], 1.0, -1.0, 1.0)  # negative acceleration
    with pytest.raises(ValueError):
        parameterize([[0.0], [1.0]], 1.0, 1.0, 1.0, start_velocity=2.0)  # > vmax


def test_corner_strategy_enum_values():
    assert CornerStrategy("curvature") is CornerStrategy.CURVATURE
    assert CornerStrategy("stop") is CornerStrategy.STOP
