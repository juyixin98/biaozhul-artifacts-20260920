import numpy as np
import pytest

from pgo.se2 import compose, inverse, between, wrap_angle, rotation2


def test_wrap_angle_range():
    vals = np.linspace(-10 * np.pi, 10 * np.pi, 1001)
    w = wrap_angle(vals)
    assert np.all(w >= -np.pi) and np.all(w < np.pi)
    # identity inside the branch
    assert wrap_angle(0.5) == pytest.approx(0.5)
    assert wrap_angle(np.pi) == pytest.approx(-np.pi)  # wraps to -pi side
    assert wrap_angle(-np.pi) == pytest.approx(-np.pi)
    assert wrap_angle(3 * np.pi) == pytest.approx(-np.pi)


def test_compose_inverse_identity():
    rng = np.random.default_rng(0)
    for _ in range(50):
        p = rng.uniform(-5, 5, 3)
        ident = compose(p, inverse(p))
        assert ident[:2] == pytest.approx([0.0, 0.0], abs=1e-12)
        assert ident[2] == pytest.approx(0.0, abs=1e-12)


def test_compose_matches_matrix_product():
    rng = np.random.default_rng(1)
    for _ in range(50):
        a, b = rng.uniform(-5, 5, 3), rng.uniform(-5, 5, 3)
        c = compose(a, b)
        Ta = np.eye(3)
        Ta[:2, :2] = rotation2(a[2])
        Ta[:2, 2] = a[:2]
        Tb = np.eye(3)
        Tb[:2, :2] = rotation2(b[2])
        Tb[:2, 2] = b[:2]
        Tc = Ta @ Tb
        assert c[:2] == pytest.approx(Tc[:2, 2], abs=1e-12)
        assert c[2] == pytest.approx(np.arctan2(Tc[1, 0], Tc[0, 0]), abs=1e-12)


def test_between_roundtrip():
    rng = np.random.default_rng(2)
    for _ in range(50):
        a, b = rng.uniform(-5, 5, 3), rng.uniform(-5, 5, 3)
        rel = between(a, b)
        b2 = compose(a, rel)
        assert b2[:2] == pytest.approx(b[:2], abs=1e-10)
        assert wrap_angle(b2[2] - b[2]) == pytest.approx(0.0, abs=1e-10)


def test_inverse_translation_only():
    p = np.array([3.0, 4.0, 0.0])
    inv = inverse(p)
    assert inv == pytest.approx([-3.0, -4.0, 0.0])
