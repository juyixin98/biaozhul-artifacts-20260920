"""Closed-form continuous collision solver for two circles.

Math
----
Robot and obstacle centers move linearly:

    p_r(t) = p_r0 + v_r t
    p_o(t) = p_o0 + v_o t

In the obstacle's frame the relative position is

    d(t) = d0 + v t,  d0 = p_r0 - p_o0,  v = v_r - v_o

Contact happens when |d(t)| = R = r_robot + r_obstacle, i.e. when

    f(t) = (v·v)t² + 2(d0·v)t + (d0·d0 - R²) = 0

    a = v·v,  b = 2 d0·v,  c = d0·d0 - R²

The earliest contact time over the closed window [t_min, t_max] is the
smallest root of f inside the window, or the window start when the
circles already overlap there.

Interval closure rules
----------------------
- The time window is CLOSED on both ends: a root exactly at t_min or
  t_max counts as contact (within a small absolute tolerance).
- Tangency (discriminant = 0, a single touching root) counts as
  contact, reported with status TANGENT.
- Initial overlap (f(t_min) < 0) reports contact at t_min with status
  ALREADY_OVERLAPPING; the entry root is meaningless in that case.
- Identical velocities (a = 0) reduce f to a constant: either the
  circles overlap at t_min (contact at t_min) or they never meet.
- No discrete sampling is performed anywhere: the answer is the exact
  root of the quadratic, so arbitrarily fast relative motion (tunneling
  between sample times) is always detected.
"""

from __future__ import annotations

import math

import numpy as np

from ccd.models import CircleBody, ContactResult, ContactStatus

# Relative tolerance on the discriminant. Below this the quadratic is
# treated as having a double root (tangency).
_DISC_REL_TOL = 1e-9

# Absolute tolerance for accepting a root that lies just outside the
# closed window (floating-point noise on the boundary).
_TIME_TOL = 1e-9

# Threshold under which a coefficient is treated as exactly zero.
_COEFF_ZERO = 1e-15


def quadratic_coefficients(robot: CircleBody, obstacle: CircleBody) -> tuple[float, float, float]:
    """Return (a, b, c) of the relative-distance-squared quadratic.

    f(t) = a t² + b t + c where f(t) = 0 marks contact, f(t) < 0 means
    the circles overlap, and c = f(0) is the signed squared-distance
    margin at t = 0.
    """
    d0 = robot.position - obstacle.position
    v = robot.velocity - obstacle.velocity
    radius_sum = robot.radius + obstacle.radius
    a = float(v @ v)
    b = 2.0 * float(d0 @ v)
    c = float(d0 @ d0) - radius_sum**2
    return a, b, c


def _root_in_window(t: float, t_min: float, t_max: float) -> float | None:
    """Accept a root inside the closed window, clamping boundary noise."""
    if t < t_min - _TIME_TOL or t > t_max + _TIME_TOL:
        return None
    return min(max(t, t_min), t_max)


def _result(status: ContactStatus, t: float, robot: CircleBody, obstacle: CircleBody) -> ContactResult:
    distance = float(np.linalg.norm(robot.center_at(t) - obstacle.center_at(t)))
    return ContactResult(status=status, time=t, distance_at_contact=distance)


_NO_CONTACT = ContactResult(status=ContactStatus.NO_COLLISION, time=None, distance_at_contact=None)


def _solve_from_zero(robot: CircleBody, obstacle: CircleBody, t_max: float) -> ContactResult:
    """Solve the query on the closed window [0, t_max]."""
    a, b, c = quadratic_coefficients(robot, obstacle)
    radius_sum = robot.radius + obstacle.radius

    # --- Initial configuration at t = 0 -----------------------------------
    # c = |d0|² - R². c < 0: overlap, c = 0: touching.
    c_tol = _COEFF_ZERO * max(1.0, radius_sum**2)
    if c < -c_tol:
        return _result(ContactStatus.ALREADY_OVERLAPPING, 0.0, robot, obstacle)
    if c <= c_tol:
        return _result(ContactStatus.TANGENT, 0.0, robot, obstacle)

    # --- No relative motion --------------------------------------------------
    if a <= _COEFF_ZERO:
        # Relative position is constant and the circles do not touch at
        # t = 0, so they never touch.
        return _NO_CONTACT

    # --- Quadratic roots -----------------------------------------------------
    disc = b * b - 4.0 * a * c
    disc_tol = _DISC_REL_TOL * max(1.0, b * b, 4.0 * a * c)

    if disc < -disc_tol:
        return _NO_CONTACT

    if disc <= disc_tol:
        # Tangency: single touching root.
        t = _root_in_window(-b / (2.0 * a), 0.0, t_max)
        if t is None:
            return _NO_CONTACT
        return _result(ContactStatus.TANGENT, t, robot, obstacle)

    # Two distinct real roots; the smaller one is the entry time.
    sqrt_disc = math.sqrt(disc)
    # Numerically stable root computation (avoid catastrophic cancellation).
    q = -0.5 * (b + math.copysign(sqrt_disc, b))
    t1 = q / a
    t2 = c / q if q != 0.0 else (-b - sqrt_disc) / (2.0 * a)
    t_entry = min(t1, t2)

    t = _root_in_window(t_entry, 0.0, t_max)
    if t is None:
        return _NO_CONTACT
    return _result(ContactStatus.COLLISION, t, robot, obstacle)


def earliest_contact(robot: CircleBody, obstacle: CircleBody, t_min: float = 0.0, t_max: float = math.inf) -> ContactResult:
    """Solve the continuous collision query over the closed window [t_min, t_max].

    Args:
        robot: The robot circle (position/velocity at t = 0).
        obstacle: The obstacle circle.
        t_min: Window start, must be finite and >= 0.
        t_max: Window end, must be >= t_min (may be math.inf).

    Returns:
        ContactResult with the earliest contact time, or NO_COLLISION.
    """
    if not math.isfinite(t_min) or t_min < 0.0:
        raise ValueError(f"t_min must be finite and >= 0, got {t_min}")
    if not t_max >= t_min:
        raise ValueError(f"t_max ({t_max}) must be >= t_min ({t_min})")

    if t_min == 0.0:
        return _solve_from_zero(robot, obstacle, t_max)

    # Re-anchor the linear trajectories to the window start so the query
    # reduces to [0, t_max - t_min]; this also makes initial-overlap and
    # tangency detection exact at the window start.
    robot_anchored = CircleBody(position=robot.center_at(t_min), velocity=robot.velocity, radius=robot.radius)
    obstacle_anchored = CircleBody(position=obstacle.center_at(t_min), velocity=obstacle.velocity, radius=obstacle.radius)
    shifted = _solve_from_zero(robot_anchored, obstacle_anchored, t_max - t_min)
    if shifted.time is None:
        return _NO_CONTACT
    return ContactResult(
        status=shifted.status,
        time=t_min + shifted.time,
        distance_at_contact=shifted.distance_at_contact,
    )
