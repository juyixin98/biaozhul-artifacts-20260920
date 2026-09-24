"""Unit tests for SE3 algebra: 90-degree rotation, quaternion sign flips,
and compose-then-inverse numerical error."""

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from tf_cache import SE3

SQRT2_2 = np.sqrt(2.0) / 2.0
# 90-degree rotation about the z axis, as quaternion [x, y, z, w].
Q_Z90 = np.array([0.0, 0.0, SQRT2_2, SQRT2_2])


def test_rotation_90deg_about_z():
    t = SE3.from_quat_translation(Q_Z90, [0.0, 0.0, 0.0])
    np.testing.assert_allclose(t.apply(np.array([1.0, 0.0, 0.0])), [0.0, 1.0, 0.0], atol=1e-12)
    np.testing.assert_allclose(t.apply(np.array([0.0, 1.0, 0.0])), [-1.0, 0.0, 0.0], atol=1e-12)
    # The z axis itself is invariant.
    np.testing.assert_allclose(t.apply(np.array([0.0, 0.0, 1.0])), [0.0, 0.0, 1.0], atol=1e-12)


def test_quaternion_sign_flip_is_same_rotation():
    a = SE3.from_quat_translation(Q_Z90, [1.0, 2.0, 3.0])
    b = SE3.from_quat_translation(-Q_Z90, [1.0, 2.0, 3.0])
    d_trans, d_rot = a.error_to(b)
    assert d_trans == 0.0
    assert d_rot < 1e-12


def test_slerp_between_sign_flipped_quaternions_stays_put():
    """Interpolating q -> -q must not rotate through 180 degrees."""
    a = SE3.from_quat_translation(Q_Z90, [0.0, 0.0, 0.0])
    b = SE3.from_quat_translation(-Q_Z90, [0.0, 0.0, 0.0])
    mid = a.interpolate(b, 0.5)
    _, d_rot = mid.error_to(a)
    assert d_rot < 1e-12


def test_interpolation_midpoint():
    a = SE3.from_quat_translation([0.0, 0.0, 0.0, 1.0], [0.0, 0.0, 0.0])
    b = SE3.from_quat_translation(Q_Z90, [2.0, 4.0, 6.0])
    mid = a.interpolate(b, 0.5)
    np.testing.assert_allclose(mid.translation, [1.0, 2.0, 3.0], atol=1e-12)
    # Half of a 90-degree rotation is 45 degrees.
    angle = mid.rotation.magnitude()
    assert angle == pytest.approx(np.pi / 4.0, abs=1e-12)


def test_compose_then_inverse_numerical_error():
    rng = np.random.default_rng(42)
    for _ in range(50):
        rot = Rotation.random(random_state=rng)
        trans = rng.normal(size=3)
        t = SE3(rot, trans)
        identity = t * t.inverse()
        d_trans, d_rot = identity.error_to(SE3.identity())
        assert d_trans < 1e-12
        assert d_rot < 1e-12


def test_compose_associates_with_matrix_product():
    rng = np.random.default_rng(7)
    a = SE3(Rotation.random(random_state=rng), rng.normal(size=3))
    b = SE3(Rotation.random(random_state=rng), rng.normal(size=3))
    c = SE3(Rotation.random(random_state=rng), rng.normal(size=3))
    lhs = ((a * b) * c).as_matrix()
    rhs = (a * (b * c)).as_matrix()
    np.testing.assert_allclose(lhs, rhs, atol=1e-12)
    np.testing.assert_allclose((a * b).as_matrix(), a.as_matrix() @ b.as_matrix(), atol=1e-12)
