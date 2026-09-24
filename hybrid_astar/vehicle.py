"""Vehicle footprint model.

The rectangular vehicle body is approximated by a row of equal circles
along its centerline. Collision checking then reduces to a clearance
lookup at each circle center, which is cheap against a precomputed
distance-transform map.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Vehicle:
    """Kinematic and shape parameters of the vehicle.

    Attributes:
        length: body length in meters.
        width: body width in meters.
        min_turning_radius: minimum turning radius in meters, defines the
            maximum curvature kappa_max = 1 / min_turning_radius.
        n_circles: number of coverage circles along the centerline.
        margin: extra safety margin added to the circle radius, in meters.
    """

    length: float = 2.0
    width: float = 1.0
    min_turning_radius: float = 2.0
    n_circles: int = 3
    margin: float = 0.1

    @property
    def kappa_max(self) -> float:
        """Maximum curvature (1/m) implied by the minimum turning radius."""
        return 1.0 / self.min_turning_radius

    @property
    def circle_radius(self) -> float:
        """Radius of each footprint coverage circle (includes margin)."""
        return self.width / 2.0 + self.margin

    @property
    def circle_offsets(self) -> np.ndarray:
        """Centerline offsets (m) of the coverage circles, rear to front."""
        r = self.circle_radius
        half = self.length / 2.0
        if self.n_circles == 1:
            return np.array([0.0])
        lo, hi = -(half - r), half - r
        if hi < lo:  # vehicle shorter than its width: single center circle
            return np.array([0.0])
        return np.linspace(lo, hi, self.n_circles)

    def circle_centers(self, x: float, y: float, theta: float) -> np.ndarray:
        """World-frame centers of the coverage circles at a pose.

        Returns an (n_circles, 2) array.
        """
        c, s = math.cos(theta), math.sin(theta)
        offs = self.circle_offsets
        return np.column_stack([x + offs * c, y + offs * s])
