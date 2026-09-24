"""Direct unit tests for the numerical primitives (no HTTP layer)."""

import numpy as np
import pytest

from app.alignment import align_trajectories
from app.errors import DegenerateAlignmentError
from app.rotation import angular_distance, matrix_to_quat, quat_to_matrix
from conftest import qz, zrot_matrix


def test_quaternion_double_cover_and_normalization():
    R1 = quat_to_matrix(np.array([0.0, 0.0, 0.0, 1.0]))  # q
    R2 = quat_to_matrix(np.array([0.0, 0.0, 0.0, -1.0]))  # -q
    assert np.allclose(R1, R2, atol=1e-12)
    # un-normalized input is accepted and normalized
    R3 = quat_to_matrix(np.array([0.0, 0.0, 0.0, 5.0]))
    assert np.allclose(R1, R3)


def test_zero_quaternion_rejected():
    from app.errors import InvalidOrientationError

    with pytest.raises(InvalidOrientationError):
        quat_to_matrix(np.zeros(4))


@pytest.mark.parametrize("angle", [0.0, 0.37, np.pi / 2, np.pi - 1e-6, np.pi])
def test_angular_distance_z_rotations(angle):
    R = zrot_matrix(angle)
    assert angular_distance(R, R) == pytest.approx(0.0, abs=1e-12)
    assert angular_distance(np.eye(3), R) == pytest.approx(angle, abs=1e-10)


def test_angular_distance_branch_cut():
    # +pi-0.05 and -pi+0.05 are only 0.1 rad apart on SO(3).
    Rp = zrot_matrix(np.pi - 0.05)
    Rm = zrot_matrix(-np.pi + 0.05)
    assert angular_distance(Rp, Rm) == pytest.approx(0.1, abs=1e-10)


def test_angular_distance_symmetry_and_range():
    rng = np.random.default_rng(0)
    for _ in range(20):
        a = rng.uniform(-np.pi, np.pi, size=3)
        b = rng.uniform(-np.pi, np.pi, size=3)
        Ra = quat_to_matrix(quat_from_axis_angles(a))
        Rb = quat_to_matrix(quat_from_axis_angles(b))
        d = angular_distance(Ra, Rb)
        assert 0.0 <= d <= np.pi + 1e-12
        assert d == pytest.approx(angular_distance(Rb, Ra), abs=1e-12)


def quat_from_axis_angles(rpy):
    """Small helper: quaternion from fixed-axis extrinsic x,y,z angles."""
    q = np.array([1.0, 0.0, 0.0, 0.0])
    for axis, ang in zip(range(3), rpy):
        half = ang / 2
        qa = np.array([np.cos(half), 0.0, 0.0, 0.0])
        qa[axis + 1] = np.sin(half)
        q = quat_mul(q, qa)
    return q


def quat_mul(a, b):
    w1, x1, y1, z1 = a
    w2, x2, y2, z2 = b
    return np.array([
        w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
        w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
        w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
        w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
    ])


def test_rigid_alignment_recovers_known_transform():
    rng = np.random.default_rng(42)
    P = rng.uniform(-3, 3, size=(30, 3))
    # build a proper rotation from a random quaternion
    q = rng.normal(size=4)
    q /= np.linalg.norm(q)
    R_true = quat_to_matrix(q)
    t_true = np.array([1.7, -2.3, 0.9])
    Q = P @ R_true.T + t_true

    a = align_trajectories(P, Q, with_scale=False)
    assert a.scale == 1.0
    assert np.allclose(a.rotation, R_true, atol=1e-9)
    assert np.allclose(a.translation, t_true, atol=1e-9)
    aligned = P @ a.rotation.T + a.translation
    assert np.allclose(aligned, Q, atol=1e-9)


def test_similarity_alignment_recovers_scale_both_directions():
    rng = np.random.default_rng(7)
    G = rng.uniform(-2, 2, size=(25, 3))
    R_gt_to_est = zrot_matrix(0.6)  # rotation baked into the estimate
    t_true = np.array([0.5, 0.5, 0.0])
    # estimate = 1.25 * R @ GT + t  ->  est->GT scale is 0.8, rotation R^T
    E = 1.25 * (G @ R_gt_to_est.T) + t_true
    a = align_trajectories(E, G, with_scale=True)
    assert a.scale == pytest.approx(0.8, abs=1e-9)
    assert np.allclose(a.rotation, R_gt_to_est.T, atol=1e-9)
    aligned = a.scale * (E @ a.rotation.T) + a.translation
    assert np.allclose(aligned, G, atol=1e-9)

    # reverse: estimate at 0.5x GT -> est->GT scale is 2.0
    E2 = 0.5 * G.copy()
    a2 = align_trajectories(E2, G, with_scale=True)
    assert a2.scale == pytest.approx(2.0, abs=1e-9)
    assert np.allclose(a2.scale * (E2 @ a2.rotation.T) + a2.translation,
                       G, atol=1e-9)


def test_alignment_rejects_degenerate_inputs():
    # single point
    with pytest.raises(DegenerateAlignmentError):
        align_trajectories(np.zeros((1, 3)), np.zeros((1, 3)))
    # coincident points
    P = np.tile(np.array([1.0, 2.0, 3.0]), (5, 1))
    Q = np.array([[float(i), 0.0, 0.0] for i in range(5)])
    with pytest.raises(DegenerateAlignmentError):
        align_trajectories(P, Q)
    # collinear centered geometry
    P = np.array([[float(i), 0.0, 0.0] for i in range(6)])
    Q = P + np.array([0.3, 1.0, 0.0])
    with pytest.raises(DegenerateAlignmentError):
        align_trajectories(P, Q)


def test_matrix_quat_roundtrip():
    for ang in [0.1, 1.2, 3.0]:
        R = zrot_matrix(ang)
        assert np.allclose(quat_to_matrix(matrix_to_quat(R)), R, atol=1e-10)
