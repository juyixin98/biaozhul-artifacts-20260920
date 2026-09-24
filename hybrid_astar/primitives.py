"""Motion primitives for a curvature-bounded (Dubins-style, bidirectional)
vehicle.

A primitive is a constant-curvature arc of fixed length, driven either
forward (gear=+1) or in reverse (gear=-1). Integration along the arc is
exact (closed form), and the arc is densely sampled for collision
checking.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

TWO_PI = 2.0 * math.pi


def wrap_angle(theta: float) -> float:
    """Wrap an angle to (-pi, pi]."""
    return (theta + math.pi) % TWO_PI - math.pi


def integrate(x: float, y: float, theta: float, kappa: float, dist: float):
    """Exact constant-curvature integration.

    Args:
        x, y, theta: start pose.
        kappa: signed curvature (1/m); 0 means straight.
        dist: signed arc length (m); negative drives in reverse.

    Returns:
        (x, y, theta) end pose.
    """
    if abs(kappa) < 1e-12:
        return x + dist * math.cos(theta), y + dist * math.sin(theta), theta
    theta_end = theta + kappa * dist
    x_end = x + (math.sin(theta_end) - math.sin(theta)) / kappa
    y_end = y - (math.cos(theta_end) - math.cos(theta)) / kappa
    return x_end, y_end, theta_end


@dataclass(frozen=True)
class Primitive:
    """A single motion primitive template."""

    kappa: float  # signed curvature (1/m)
    gear: int  # +1 forward, -1 reverse
    length: float  # arc length (m, positive)

    @property
    def signed_length(self) -> float:
        return self.gear * self.length


def build_primitives(
    kappa_max: float,
    length: float,
    allow_reverse: bool = True,
    steer_fractions=(0.0, 0.5, 1.0),
) -> list[Primitive]:
    """Build the primitive set: straight/half/full curvature x gears."""
    prims: list[Primitive] = []
    gears = [1, -1] if allow_reverse else [1]
    for gear in gears:
        for frac in steer_fractions:
            if frac == 0.0:
                prims.append(Primitive(0.0, gear, length))
            else:
                prims.append(Primitive(kappa_max * frac, gear, length))
                prims.append(Primitive(-kappa_max * frac, gear, length))
    return prims


def simulate_primitive(
    x: float,
    y: float,
    theta: float,
    prim: Primitive,
    sample_step: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Roll out a primitive, densely sampled for collision checking.

    Returns:
        (xs, ys, thetas) sample arrays including the start and end pose.
    """
    s_total = prim.signed_length
    n = max(1, int(math.ceil(abs(s_total) / sample_step)))
    ds = s_total / n
    xs = np.empty(n + 1)
    ys = np.empty(n + 1)
    ths = np.empty(n + 1)
    xs[0], ys[0], ths[0] = x, y, theta
    cx, cy, ct = x, y, theta
    for i in range(1, n + 1):
        cx, cy, ct = integrate(cx, cy, ct, prim.kappa, ds)
        xs[i], ys[i], ths[i] = cx, cy, ct
    return xs, ys, ths
