"""Unit tests for kinematics and motion primitives."""

import math

import numpy as np
import pytest

from hybrid_astar.primitives import (
    build_primitives,
    integrate,
    simulate_primitive,
    wrap_angle,
)


def test_integrate_straight_forward():
    x, y, t = integrate(1.0, 2.0, math.pi / 4, 0.0, 2.0)
    assert x == pytest.approx(1.0 + 2.0 * math.cos(math.pi / 4))
    assert y == pytest.approx(2.0 + 2.0 * math.sin(math.pi / 4))
    assert t == pytest.approx(math.pi / 4)


def test_integrate_straight_reverse():
    x, y, t = integrate(0.0, 0.0, 0.0, 0.0, -1.5)
    assert x == pytest.approx(-1.5)
    assert y == pytest.approx(0.0)
    assert t == pytest.approx(0.0)


def test_integrate_quarter_circle():
    # kappa = 1/R with R = 2; arc of length pi (quarter circle? no:)
    # theta change = kappa * dist = pi/2 -> quarter circle of radius 2.
    x, y, t = integrate(0.0, 0.0, 0.0, 0.5, math.pi)
    assert t == pytest.approx(math.pi / 2)
    assert x == pytest.approx(2.0)   # R * sin(pi/2)
    assert y == pytest.approx(2.0)   # R * (1 - cos(pi/2))


def test_integrate_full_circle_returns_to_start():
    x, y, t = integrate(3.0, -1.0, 0.7, 0.5, 4.0 * math.pi)  # 2*pi/kappa
    assert x == pytest.approx(3.0)
    assert y == pytest.approx(-1.0)
    assert wrap_angle(t - 0.7) == pytest.approx(0.0)


def test_wrap_angle():
    # pi and -pi are the same angle; either representation is acceptable
    assert abs(wrap_angle(3 * math.pi)) == pytest.approx(math.pi)
    assert abs(wrap_angle(-3 * math.pi)) == pytest.approx(math.pi)
    assert wrap_angle(0.3) == pytest.approx(0.3)
    assert wrap_angle(2 * math.pi + 0.3) == pytest.approx(0.3)


def test_build_primitives_counts_and_curvature_bound():
    kmax = 0.5
    prims = build_primitives(kmax, length=1.0, allow_reverse=True)
    # 5 forward (straight, +-half, +-full) + 5 reverse
    assert len(prims) == 10
    assert all(abs(p.kappa) <= kmax for p in prims)
    assert {p.gear for p in prims} == {1, -1}
    fwd_only = build_primitives(kmax, length=1.0, allow_reverse=False)
    assert len(fwd_only) == 5
    assert all(p.gear == 1 for p in fwd_only)


def test_simulate_primitive_sampling_and_endpoint():
    prim = build_primitives(0.5, 1.0)[0]  # straight forward
    xs, ys, ths = simulate_primitive(0.0, 0.0, 0.0, prim, sample_step=0.25)
    assert len(xs) == 5  # 4 steps + start
    assert xs[-1] == pytest.approx(1.0)
    # dense spacing never exceeds the requested step
    d = np.hypot(np.diff(xs), np.diff(ys))
    assert np.all(d <= 0.25 + 1e-9)


def test_simulate_reverse_curved_stays_on_circle():
    prims = [p for p in build_primitives(0.5, 1.0) if p.gear == -1 and p.kappa > 0]
    prim = prims[0]
    xs, ys, ths = simulate_primitive(0.0, 0.0, 0.0, prim, sample_step=0.1)
    # every sample lies on the arc circle of radius 1/kappa around the ICC
    r = 1.0 / prim.kappa
    icc_x, icc_y = 0.0 - r * math.sin(0.0), 0.0 + r * math.cos(0.0)
    dist = np.hypot(xs - icc_x, ys - icc_y)
    assert np.allclose(dist, r, atol=1e-9)
