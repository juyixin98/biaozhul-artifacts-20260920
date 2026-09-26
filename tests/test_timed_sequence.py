"""TimedTransformSequence：插值、严格覆盖（不外推）、缺口检测。"""

import numpy as np
import pytest

from transform_tree.errors import (
    DuplicateTimestampError,
    InvalidKeyframeError,
    TimeGapError,
    TimeNotCoveredError,
)
from transform_tree.timed_sequence import Keyframe, StaticTransformProvider, TimedTransformSequence
from transform_tree.transform import Transform


def _kf(t: float, angle_deg: float, translation=(0, 0, 0)) -> Keyframe:
    angle = np.deg2rad(angle_deg)
    return Keyframe(t, Transform.from_quaternion([np.cos(angle / 2), 0, 0, np.sin(angle / 2)], translation))


def test_keyframe_exact_hit_returns_pose():
    seq = TimedTransformSequence([_kf(0.0, 0.0, (1, 2, 3)), _kf(2.0, 90.0, (3, 2, 1))])
    t = seq.lookup(2.0)
    np.testing.assert_allclose(t.translation, [3, 2, 1])
    # Rz(90°)·(1,0,0) + (3,2,1) = (0,1,0) + (3,2,1) = (3,3,1)
    np.testing.assert_allclose(t.transform_point([1, 0, 0]), [3, 3, 1], atol=1e-12)


def test_translation_linearly_interpolated():
    seq = TimedTransformSequence([_kf(0.0, 0.0, (0, 0, 0)), _kf(2.0, 90.0, (2, 4, 6))])
    t = seq.lookup(1.0)
    np.testing.assert_allclose(t.translation, [1, 2, 3])


def test_rotation_slerp_interpolated_to_45_degrees():
    seq = TimedTransformSequence([_kf(0.0, 0.0), _kf(2.0, 90.0)])
    t = seq.lookup(1.0)
    np.testing.assert_allclose(t.transform_point([1, 0, 0]), [np.cos(np.pi / 4), np.sin(np.pi / 4), 0], atol=1e-12)


def test_lookup_before_first_keyframe_is_rejected():
    # 验收点：不得把最新值（或任何帧）默认当作历史值。
    seq = TimedTransformSequence([_kf(0.0, 0.0), _kf(2.0, 90.0)])
    with pytest.raises(TimeNotCoveredError):
        seq.lookup(-0.001)


def test_lookup_after_last_keyframe_is_rejected():
    # 验收点：不允许外推，末帧值不会被用于未来时刻。
    seq = TimedTransformSequence([_kf(0.0, 0.0), _kf(2.0, 90.0)])
    with pytest.raises(TimeNotCoveredError):
        seq.lookup(2.001)


def test_lookup_at_endpoints_is_allowed():
    seq = TimedTransformSequence([_kf(0.0, 10.0), _kf(2.0, 90.0)])
    assert seq.lookup(0.0).is_close(seq.lookup(1e-13), atol=1e-9)
    seq.lookup(2.0)  # 不抛异常即可


def test_gap_between_bracketing_keyframes_is_rejected():
    seq = TimedTransformSequence(
        [_kf(0.0, 0.0), _kf(0.5, 5.0), _kf(4.0, 90.0), _kf(4.5, 95.0)], max_gap=0.6
    )
    with pytest.raises(TimeGapError):
        seq.lookup(2.0)  # 落在 0.5 与 4.0 之间，缺口 3.5s > 0.6s
    # 缺口外的正常区间仍可查询（间隔 0.5s <= 0.6s）。
    seq.lookup(0.25)
    seq.lookup(4.25)


def test_gap_check_can_be_overridden_per_lookup():
    seq = TimedTransformSequence([_kf(0.0, 0.0), _kf(4.0, 90.0)], max_gap=0.6)
    with pytest.raises(TimeGapError):
        seq.lookup(2.0)
    # 显式放宽后允许插值。
    t = seq.lookup(2.0, max_gap=10.0)
    np.testing.assert_allclose(t.transform_point([1, 0, 0]), [np.cos(np.pi / 4), np.sin(np.pi / 4), 0], atol=1e-12)


def test_duplicate_timestamp_rejected():
    with pytest.raises(DuplicateTimestampError):
        TimedTransformSequence([_kf(1.0, 0.0), _kf(1.0, 10.0)])


def test_unsorted_keyframes_rejected():
    with pytest.raises(InvalidKeyframeError):
        TimedTransformSequence([_kf(2.0, 0.0), _kf(1.0, 10.0)])


def test_empty_sequence_rejected():
    with pytest.raises(InvalidKeyframeError):
        TimedTransformSequence([])


def test_non_finite_timestamp_rejected():
    with pytest.raises(InvalidKeyframeError):
        TimedTransformSequence([_kf(np.nan, 0.0)])


def test_static_provider_returns_same_transform_at_any_time():
    pose = Transform.from_quaternion([np.cos(0.3), 0, 0, np.sin(0.3)], [1, 0, 0])
    provider = StaticTransformProvider(pose)
    assert provider.lookup(-100.0).is_close(pose)
    assert provider.lookup(100.0).is_close(pose)
