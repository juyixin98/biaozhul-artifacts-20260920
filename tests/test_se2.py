"""Unit tests for the SE(2) pose algebra."""

import numpy as np

from pose_graph.se2 import between, compose, inverse, wrap_angle


def test_wrap_angle_range():
    angles = np.linspace(-10 * np.pi, 10 * np.pi, 1001)
    wrapped = wrap_angle(angles)
    assert np.all(wrapped >= -np.pi)
    assert np.all(wrapped < np.pi)


def test_wrap_angle_known_values():
    assert np.isclose(wrap_angle(3 * np.pi), -np.pi)
    assert np.isclose(wrap_angle(0.3), 0.3)
    assert np.isclose(wrap_angle(-np.pi / 2), -np.pi / 2)


def test_compose_identity():
    rng = np.random.default_rng(1)
    zero = np.zeros(3)
    for _ in range(50):
        p = rng.normal(size=3)
        assert np.allclose(compose(p, zero), p, atol=1e-12)
        assert np.allclose(compose(zero, p), p, atol=1e-12)


def test_inverse_roundtrip():
    rng = np.random.default_rng(2)
    for _ in range(50):
        p = rng.normal(size=3)
        assert np.allclose(compose(p, inverse(p)), np.zeros(3), atol=1e-12)
        assert np.allclose(compose(inverse(p), p), np.zeros(3), atol=1e-12)


def test_between_roundtrip():
    rng = np.random.default_rng(3)
    for _ in range(50):
        a, b = rng.normal(size=3), rng.normal(size=3)
        assert np.allclose(compose(a, between(a, b)), b, atol=1e-12)


def test_compose_associativity():
    rng = np.random.default_rng(4)
    for _ in range(50):
        a, b, c = rng.normal(size=3), rng.normal(size=3), rng.normal(size=3)
        lhs = compose(compose(a, b), c)
        rhs = compose(a, compose(b, c))
        assert np.allclose(lhs[:2], rhs[:2], atol=1e-10)
        assert np.isclose(wrap_angle(lhs[2] - rhs[2]), 0.0, atol=1e-10)
