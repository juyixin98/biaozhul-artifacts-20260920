"""Unit tests for rotation/SE(3) geometry."""

from __future__ import annotations

import numpy as np

from app.alignment import umeyama
from app.errors import EvaluationError
from app.geometry import (
    normalize_quat,
    quat_to_rot,
    relative_pose,
    rotation_angle,
    rotation_distance_rad,
    rot_to_quat,
)
from tests._helpers import q_from_axis_angle, q_identity

RT = 180.0 / np.pi


def test_quaternion_double_cover_normalized():
    q = q_from_axis_angle(np.array([0.0, 0.0, 1.0]), 1.234)
    assert np.allclose(normalize_quat(q), normalize_quat(-q))


def test_rotation_distance_wraparound_zero():
    # Angles +pi and -pi about the same axis are the same rotation, though
    # written with opposite quaternion signs (the -1/1 representation seam).
    R_plus = quat_to_rot(q_from_axis_angle(np.array([0.0, 1.0, 0.0]), np.pi))
    R_minus = quat_to_rot(q_from_axis_angle(np.array([0.0, 1.0, 0.0]), -np.pi))
    assert rotation_distance_rad(R_plus, R_minus) < 1e-10


def test_rotation_distance_known_angles():
    R0 = np.eye(3)
    for ang in [0.1, 0.7, 1.5, 2.9, np.pi]:
        R = quat_to_rot(q_from_axis_angle(np.array([1.0, 0.0, 0.0]), ang))
        assert abs(rotation_distance_rad(R0, R) - ang) < 1e-10


def test_rot_quat_roundtrip():
    for ang in [0.01, 0.8, 2.5, np.pi - 1e-6, np.pi]:
        q = normalize_quat(q_from_axis_angle(np.array([0.3, -0.5, 0.8]), ang))
        q2 = rot_to_quat(quat_to_rot(q))
        assert np.allclose(q2, q, atol=1e-9)


def test_relative_pose_motion_composition():
    Ra = quat_to_rot(q_from_axis_angle(np.array([0.0, 0.0, 1.0]), 0.5))
    Rb = quat_to_rot(q_from_axis_angle(np.array([0.0, 1.0, 0.0]), 1.0))
    ta = np.array([1.0, 2.0, 0.0])
    tb = np.array([1.0, 3.0, 0.0])
    Rr, tr = relative_pose(Ra, ta, Rb, tb)
    # Relative rotation = Ra^T Rb; relative translation tr = Ra^T (tb - ta).
    assert np.linalg.norm(Rr - Ra.T @ Rb) < 1e-10
    assert np.allclose(tr, Ra.T @ (tb - ta), atol=1e-10)


def test_zero_quaternion_rejected():
    try:
        normalize_quat(np.array([0.0, 0.0, 0.0, 0.0]))
    except EvaluationError as e:
        assert e.code == "INVALID_POSE"
    else:
        raise AssertionError("zero quaternion should raise")


def test_umeyama_recovers_known_transform():
    rng = np.random.default_rng(0)
    src = rng.normal(size=(30, 3))
    axis = np.array([0.4, -0.2, 1.0])
    axis /= np.linalg.norm(axis)
    R = quat_to_rot(q_from_axis_angle(axis, 0.9))
    t = np.array([1.5, -2.0, 0.3])
    s, dst = 2.5, (2.5 * src @ R.T) + t

    rigid = umeyama(src, dst, with_scale=False)
    assert abs(rigid.scale - 1.0) < 1e-12
    # Umeyama's rotation estimate is scale-independent, so R is recovered;
    # the 2.5x scale mismatch surfaces in the translation residual instead.
    assert np.linalg.norm(rigid.rotation - R) < 1e-9
    assert np.linalg.norm(rigid.apply(src) - dst) > 1.0

    sim = umeyama(src, dst, with_scale=True)
    assert abs(sim.scale - s) < 1e-9
    assert np.linalg.norm(sim.rotation - R) < 1e-9
    assert np.allclose(sim.translation, t, atol=1e-9)
    assert np.allclose(sim.apply(src), dst, atol=1e-9)
    assert abs(np.linalg.det(sim.rotation) - 1.0) < 1e-12


def test_umeyama_planar_points_allowed():
    # A planar (rank-2) rectangle still fixes a rigid transform.
    src = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]], dtype=float)
    R = quat_to_rot(q_from_axis_angle(np.array([0.0, 0.0, 1.0]), 0.3))
    dst = src @ R.T + np.array([2.0, -1.0, 0.5])
    tr = umeyama(src, dst, with_scale=False)
    assert np.allclose(tr.apply(src), dst, atol=1e-9)


def test_umeyama_collinear_rejected():
    src = np.array([[0, 0, 0], [1, 0, 0], [2, 0, 0], [3, 0, 0]], dtype=float)
    dst = src + np.array([0.0, 0.0, 5.0])
    try:
        umeyama(src, dst, with_scale=False)
    except EvaluationError as e:
        assert e.code == "ALIGNMENT_DEGENERATE"
    else:
        raise AssertionError("collinear alignment should be rejected")
