"""折线几何测试：长度、转角、重复点折叠与零长段安全。"""

import numpy as np
import pytest

from trajectory_planning.path import (
    build_polyline,
    segment_lengths,
    turning_angles,
    unit_tangents,
)


def test_segment_lengths_basic():
    pts = np.array([[0.0, 0.0], [3.0, 0.0], [3.0, 4.0]])
    lengths = segment_lengths(pts)
    np.testing.assert_allclose(lengths, [3.0, 4.0])


def test_build_polyline_folds_adjacent_duplicates():
    poly = build_polyline([[0, 0], [1, 0], [1, 0], [2, 0]])
    assert poly.n_nodes == 3
    assert poly.removed_duplicates == 1
    np.testing.assert_allclose(poly.lengths, [1.0, 1.0])


def test_zero_length_segment_no_division():
    # 直接构造含零长段的点：unit_tangents 必须返回零向量而非 NaN
    pts = np.array([[1.0, 0.0], [1.0, 0.0], [2.0, 0.0]])
    lengths = segment_lengths(pts)
    assert lengths[0] == 0.0
    tangents = unit_tangents(pts, lengths)
    assert np.all(np.isfinite(tangents))
    np.testing.assert_allclose(tangents[0], [0.0, 0.0])
    np.testing.assert_allclose(tangents[1], [1.0, 0.0])
    angles = turning_angles(pts, lengths)
    assert np.all(np.isfinite(angles))


def test_turning_angles_right_angle_and_straight():
    pts = np.array([[0.0, 0.0], [2.0, 0.0], [2.0, 2.0], [4.0, 2.0]])
    angles = turning_angles(pts, segment_lengths(pts))
    np.testing.assert_allclose(angles, [np.pi / 2, np.pi / 2])


def test_turning_angle_cusp():
    pts = np.array([[0.0, 0.0], [1.0, 0.0], [0.0, 0.0]])
    angles = turning_angles(pts, segment_lengths(pts))
    np.testing.assert_allclose(angles, [np.pi])


@pytest.mark.parametrize(
    "points,msg",
    [
        ([[0.0, 0.0]], "至少"),
        ([[0.0, np.nan], [1.0, 0.0]], "NaN"),
        ([[0.0, 0.0], [0.0, 0.0]], "完全重合"),
    ],
)
def test_invalid_points_raise(points, msg):
    with pytest.raises(ValueError, match=msg):
        build_polyline(points)
