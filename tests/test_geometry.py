"""Tests for SE(2) geometry helpers."""

import math

import numpy as np
import pytest

from scan_matching.geometry import (
    SE2Pose,
    apply_transform,
    compose,
    inverse,
    pose_error,
)


def test_identity_pose_leaves_points_unchanged():
    points = np.array([[1.0, 2.0], [-3.0, 0.5]])
    np.testing.assert_allclose(apply_transform(points, SE2Pose()), points)


def test_apply_transform_rotation_90_degrees():
    pose = SE2Pose(x=1.0, y=2.0, theta=math.pi / 2)
    out = apply_transform(np.array([[1.0, 0.0]]), pose)
    np.testing.assert_allclose(out, [[1.0, 3.0]], atol=1e-12)


def test_compose_then_inverse_recovers_original():
    rng = np.random.default_rng(0)
    points = rng.normal(size=(20, 2))
    a = SE2Pose(x=0.3, y=-0.2, theta=0.4)
    b = SE2Pose(x=-1.0, y=0.5, theta=-0.7)
    moved = apply_transform(points, compose(a, b))
    restored = apply_transform(moved, inverse(compose(a, b)))
    np.testing.assert_allclose(restored, points, atol=1e-10)


def test_inverse_is_true_inverse():
    pose = SE2Pose(x=1.5, y=-2.0, theta=0.9)
    identity = compose(pose, inverse(pose))
    assert identity.x == pytest.approx(0.0, abs=1e-12)
    assert identity.y == pytest.approx(0.0, abs=1e-12)
    assert identity.theta == pytest.approx(0.0, abs=1e-12)


def test_pose_error_wraps_angle():
    a = SE2Pose(theta=math.pi - 0.01)
    b = SE2Pose(theta=-math.pi + 0.01)
    assert pose_error(a, b)["rotation"] == pytest.approx(0.02, abs=1e-9)


def test_pose_roundtrip_through_dict():
    pose = SE2Pose(x=1.0, y=-2.0, theta=0.3)
    assert SE2Pose.from_dict(pose.to_dict()) == pose
