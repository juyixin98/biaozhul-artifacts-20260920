"""SE(3) 数学内核测试：exp/log、伴随、四元数、数值边界。"""

import numpy as np
import pytest

from app.se3 import (
    adjoint,
    inv_tf,
    make_tf,
    quat_to_rot,
    rot_to_quat,
    se3_exp,
    se3_log,
    skew,
    so3_exp,
    so3_log,
)


@pytest.mark.parametrize("scale", [1e-12, 1e-9, 1e-7, 1e-4, 0.01, 0.1, 0.5, 2.5, 3.0])
def test_se3_exp_log_roundtrip(scale):
    rng = np.random.default_rng(int(scale * 1e6) % 2**31)
    for _ in range(20):
        xi = rng.normal(size=6)
        n = np.linalg.norm(xi[3:])
        xi[3:] *= scale / max(n, 1e-30)
        xi[:3] *= scale * 0.5
        assert np.allclose(se3_log(se3_exp(xi)), xi, atol=1e-9)


@pytest.mark.parametrize("angle", [1e-12, 1e-7, 1e-4, 0.01, 1.0, 2.5, 3.0, np.pi])
def test_so3_exp_log(angle):
    rng = np.random.default_rng(42)
    for _ in range(15):
        ax = rng.normal(size=3)
        ax /= np.linalg.norm(ax)
        R = so3_exp(ax * angle)
        phi = so3_log(R)
        # 近 pi 存在固有数值不可区分性，放宽到理论上界
        tol = 3e-9 if abs(angle - np.pi) < 1e-5 else 1e-10
        assert np.allclose(so3_exp(phi), R, atol=tol)
        d = min(np.linalg.norm(phi - ax * angle), np.linalg.norm(phi + ax * angle))
        assert d < (1e-6 if abs(angle - np.pi) < 1e-5 else 1e-10)


def test_adjoint_identity():
    rng = np.random.default_rng(0)
    A = se3_exp(rng.normal(scale=0.5, size=6))
    B = se3_exp(rng.normal(scale=0.5, size=6))
    assert np.allclose(adjoint(A @ B), adjoint(A) @ adjoint(B))
    xi = rng.normal(scale=0.1, size=6)
    # T Exp(v) = Exp(Ad_T v) T
    assert np.allclose(A @ se3_exp(xi), se3_exp(adjoint(A) @ xi) @ A)


def test_inverse():
    rng = np.random.default_rng(1)
    for _ in range(20):
        T = se3_exp(rng.normal(scale=1.0, size=6))
        assert np.allclose(inv_tf(T) @ T, np.eye(4), atol=1e-12)
        assert np.allclose(T @ inv_tf(T), np.eye(4), atol=1e-12)


def test_skew():
    v = np.array([1.0, 2.0, 3.0])
    K = skew(v)
    assert np.allclose(K, -K.T)
    assert np.allclose(K @ np.array([0.0, 0.0, 1.0]), np.cross(v, [0, 0, 1]))


@pytest.mark.parametrize("angle", [0.1, 1.2, 2.5])
def test_quaternion_roundtrip(angle):
    rng = np.random.default_rng(7)
    ax = rng.normal(size=3)
    ax /= np.linalg.norm(ax)
    R = so3_exp(ax * angle)
    q = rot_to_quat(R)
    assert abs(np.linalg.norm(q) - 1) < 1e-12
    assert np.allclose(quat_to_rot(q), R, atol=1e-10)
    # 恒等四元数
    assert np.allclose(quat_to_rot([1, 0, 0, 0]), np.eye(3))


def test_make_tf():
    R = so3_exp(np.array([0.0, 0.0, 0.5]))
    t = np.array([1.0, 2.0, 3.0])
    T = make_tf(t, R)
    assert np.allclose(T[:3, :3], R)
    assert np.allclose(T[:3, 3], t)
    assert np.allclose(T[3], [0, 0, 0, 1])
