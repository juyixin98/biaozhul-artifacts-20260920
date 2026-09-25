import numpy as np
import pytest

from trajvel.geometry import (
    as_points,
    dedupe_points,
    segment_lengths,
    turning_angles,
    unit_directions,
)


def test_segment_lengths_basic():
    points = np.array([[0.0, 0.0], [3.0, 4.0], [3.0, 4.0], [6.0, 8.0]])
    lengths = segment_lengths(points)
    np.testing.assert_allclose(lengths, [5.0, 0.0, 5.0])


def test_dedupe_removes_zero_length_segments():
    points = np.array([[0.0, 0.0], [1.0, 0.0], [1.0, 0.0], [1.0, 0.0], [2.0, 0.0]])
    deduped, removed = dedupe_points(points)
    assert removed == 2
    np.testing.assert_allclose(deduped, [[0.0, 0.0], [1.0, 0.0], [2.0, 0.0]])


def test_dedupe_keeps_final_waypoint():
    points = np.array([[0.0, 0.0], [1.0, 1.0], [1.0, 1.0]])
    deduped, removed = dedupe_points(points)
    assert removed == 1
    np.testing.assert_allclose(deduped, [[0.0, 0.0], [1.0, 1.0]])


def test_turning_angles_right_angle_corner():
    points = np.array([[0.0, 0.0], [1.0, 0.0], [1.0, 1.0]])
    angles = turning_angles(points)
    assert angles[0] == 0.0 and angles[-1] == 0.0
    assert angles[1] == pytest.approx(np.pi / 2)


def test_turning_angles_straight_line_is_zero():
    points = np.array([[0.0, 0.0], [1.0, 0.0], [2.0, 0.0]])
    np.testing.assert_allclose(turning_angles(points), [0.0, 0.0, 0.0])


def test_turning_angles_zero_length_segment_no_divide_by_zero():
    points = np.array([[0.0, 0.0], [1.0, 0.0], [1.0, 0.0], [1.0, 1.0]])
    angles = turning_angles(points)
    assert np.all(np.isfinite(angles))


def test_unit_directions_zero_length_gives_zero_vector():
    points = np.array([[0.0, 0.0], [0.0, 0.0], [3.0, 4.0]])
    lengths = segment_lengths(points)
    directions = unit_directions(points, lengths)
    np.testing.assert_allclose(directions[0], [0.0, 0.0])
    np.testing.assert_allclose(directions[1], [0.6, 0.8])


def test_as_points_rejects_bad_input():
    with pytest.raises(ValueError):
        as_points([[0.0, 0.0]])  # fewer than two points
    with pytest.raises(ValueError):
        as_points([[0.0, 0.0], [np.inf, 0.0]])  # non-finite
    with pytest.raises(ValueError):
        as_points([1.0, 2.0, 3.0])  # not 2-D
