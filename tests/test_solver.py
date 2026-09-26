"""Hand-verified tests for the closed-form collision solver.

Every expected value below is computed by hand from the quadratic

    f(t) = a t² + b t + c,  a = v·v, b = 2 d0·v, c = d0·d0 - R²

(no discrete sampling anywhere in the checks either).
"""

import math

import numpy as np
import pytest

from ccd import CircleBody, ContactStatus, earliest_contact, quadratic_coefficients


def body(x, y, vx, vy, radius):
    return CircleBody(position=np.array([x, y]), velocity=np.array([vx, vy]), radius=radius)


# ---------------------------------------------------------------------------
# Head-on collision: robot (0,0) v=(1,0) r=1, obstacle (10,0) v=-1) r=1.
# d0=(-10,0), v=(2,0), R=2
# f(t) = 4t² - 40t + 96 = 0 → t² - 10t + 24 = 0 → roots 4 and 6.
# Earliest contact: t = 4, contact distance = 2.
# ---------------------------------------------------------------------------
def test_head_on_quadratic_coefficients():
    robot = body(0, 0, 1, 0, 1.0)
    obstacle = body(10, 0, -1, 0, 1.0)
    a, b, c = quadratic_coefficients(robot, obstacle)
    assert (a, b, c) == (4.0, -40.0, 96.0)


def test_head_on_earliest_contact():
    robot = body(0, 0, 1, 0, 1.0)
    obstacle = body(10, 0, -1, 0, 1.0)
    result = earliest_contact(robot, obstacle, 0.0, 10.0)
    assert result.status is ContactStatus.COLLISION
    assert result.time == pytest.approx(4.0, abs=1e-12)
    assert result.distance_at_contact == pytest.approx(2.0, abs=1e-9)


def test_head_on_window_excludes_entry_root():
    # Window closes at t=3, before the entry root t=4: no contact.
    robot = body(0, 0, 1, 0, 1.0)
    obstacle = body(10, 0, -1, 0, 1.0)
    result = earliest_contact(robot, obstacle, 0.0, 3.0)
    assert result.status is ContactStatus.NO_COLLISION
    assert result.time is None


def test_head_root_on_closed_window_boundary():
    # Closed window: a root exactly at t_max counts.
    robot = body(0, 0, 1, 0, 1.0)
    obstacle = body(10, 0, -1, 0, 1.0)
    result = earliest_contact(robot, obstacle, 0.0, 4.0)
    assert result.status is ContactStatus.COLLISION
    assert result.time == pytest.approx(4.0, abs=1e-12)


def test_head_on_window_start_inside_overlap():
    # At t=5 the centers coincide (d = (0,0)): overlap reported at window start.
    robot = body(0, 0, 1, 0, 1.0)
    obstacle = body(10, 0, -1, 0, 1.0)
    result = earliest_contact(robot, obstacle, 5.0, 10.0)
    assert result.status is ContactStatus.ALREADY_OVERLAPPING
    assert result.time == pytest.approx(5.0, abs=1e-12)


def test_head_on_window_start_before_entry():
    # Window starts at t=2, entry root is still t=4.
    robot = body(0, 0, 1, 0, 1.0)
    obstacle = body(10, 0, -1, 0, 1.0)
    result = earliest_contact(robot, obstacle, 2.0, 10.0)
    assert result.status is ContactStatus.COLLISION
    assert result.time == pytest.approx(4.0, abs=1e-12)


# ---------------------------------------------------------------------------
# Tangency: robot (0,0) v=(1,0) r=1, obstacle (5,2) r=1.
# d0=(-5,-2), v=(1,0), R=2
# f(t) = (t-5)² + 4 - 4 = (t-5)² → double root t=5 (touch, no penetration).
# ---------------------------------------------------------------------------
def test_tangent_contact():
    robot = body(0, 0, 1, 0, 1.0)
    obstacle = body(5, 2, 0, 0, 1.0)
    result = earliest_contact(robot, obstacle, 0.0, 10.0)
    assert result.status is ContactStatus.TANGENT
    assert result.time == pytest.approx(5.0, abs=1e-12)
    assert result.distance_at_contact == pytest.approx(2.0, abs=1e-9)


def test_tangent_root_outside_window():
    robot = body(0, 0, 1, 0, 1.0)
    obstacle = body(5, 2, 0, 0, 1.0)
    result = earliest_contact(robot, obstacle, 0.0, 4.9)
    assert result.status is ContactStatus.NO_COLLISION


# ---------------------------------------------------------------------------
# Initial overlap: robot (0,0) r=1, obstacle (1.5,0) r=1, both static.
# distance = 1.5 < 2 → already overlapping at t=0.
# ---------------------------------------------------------------------------
def test_initial_overlap_static():
    robot = body(0, 0, 0, 0, 1.0)
    obstacle = body(1.5, 0, 0, 0, 1.0)
    result = earliest_contact(robot, obstacle, 0.0, 10.0)
    assert result.status is ContactStatus.ALREADY_OVERLAPPING
    assert result.time == 0.0
    assert result.distance_at_contact == pytest.approx(1.5)


def test_initial_touching_at_start_is_tangent():
    # distance exactly 2 = R → touching at t=0, reported as tangent.
    robot = body(0, 0, 0, 0, 1.0)
    obstacle = body(2.0, 0, 0, 0, 1.0)
    result = earliest_contact(robot, obstacle, 0.0, 10.0)
    assert result.status is ContactStatus.TANGENT
    assert result.time == 0.0


# ---------------------------------------------------------------------------
# Same velocity (a = 0): relative position constant.
# ---------------------------------------------------------------------------
def test_same_velocity_no_contact():
    robot = body(0, 0, 1, 1, 1.0)
    obstacle = body(5, 0, 1, 1, 1.0)
    result = earliest_contact(robot, obstacle, 0.0, 10.0)
    assert result.status is ContactStatus.NO_COLLISION


def test_same_velocity_overlapping():
    robot = body(0, 0, 1, 1, 1.0)
    obstacle = body(1.0, 0.0, 1, 1, 1.0)  # distance 1 < 2
    result = earliest_contact(robot, obstacle, 0.0, 10.0)
    assert result.status is ContactStatus.ALREADY_OVERLAPPING
    assert result.time == 0.0


# ---------------------------------------------------------------------------
# High-speed tunneling: robot (0,0) v=(1000,0) r=0.5, obstacle (500,0.5) r=0.5.
# R = 1. d0=(-500,-0.5), v=(1000,0)
# f(t) = (1000t-500)² + 0.25 - 1 = 0 → 1000t = 500 - √0.75
#        t = (500 - √0.75)/1000 ≈ 0.4991339742155614
# Any sampling with dt > 2√0.75/1000 ≈ 0.00173 would miss the overlap.
# ---------------------------------------------------------------------------
def test_high_speed_tunneling():
    robot = body(0, 0, 1000, 0, 0.5)
    obstacle = body(500, 0.5, 0, 0, 0.5)
    expected = (500.0 - math.sqrt(0.75)) / 1000.0
    result = earliest_contact(robot, obstacle, 0.0, 1.0)
    assert result.status is ContactStatus.COLLISION
    assert result.time == pytest.approx(expected, abs=1e-12)
    assert result.distance_at_contact == pytest.approx(1.0, abs=1e-9)


# ---------------------------------------------------------------------------
# Miss: robot (0,0) v=(1,0) r=1, obstacle (10,5) r=1.
# Closest approach distance = 5 > 2 → never touch.
# ---------------------------------------------------------------------------
def test_miss():
    robot = body(0, 0, 1, 0, 1.0)
    obstacle = body(10, 5, 0, 0, 1.0)
    result = earliest_contact(robot, obstacle, 0.0, 100.0)
    assert result.status is ContactStatus.NO_COLLISION


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------
def test_invalid_radius():
    with pytest.raises(ValueError):
        body(0, 0, 0, 0, -1.0)


def test_invalid_window():
    robot = body(0, 0, 1, 0, 1.0)
    obstacle = body(10, 0, 0, 0, 1.0)
    with pytest.raises(ValueError):
        earliest_contact(robot, obstacle, 5.0, 1.0)
    with pytest.raises(ValueError):
        earliest_contact(robot, obstacle, -1.0, 1.0)
