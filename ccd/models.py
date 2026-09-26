"""Data models for the CCD solver."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import numpy as np


class ContactStatus(str, Enum):
    """Outcome classification of a pairwise continuous collision query."""

    #: Relative motion carries the circles into overlap; a genuine entry
    #: time exists inside the query window.
    COLLISION = "collision"
    #: The circles touch exactly once (discriminant == 0) without
    #: penetrating, or they are exactly touching at the window start.
    TANGENT = "tangent"
    #: The circles already overlap (distance < combined radius) at the
    #: start of the time window.
    ALREADY_OVERLAPPING = "already_overlapping"
    #: No contact occurs anywhere inside the closed time window.
    NO_COLLISION = "no_collision"


@dataclass(frozen=True)
class CircleBody:
    """A circular body moving with constant velocity.

    Attributes:
        position: Center position at t = 0, shape (2,).
        velocity: Constant velocity, shape (2,).
        radius: Circle radius, must be > 0.
    """

    position: np.ndarray
    velocity: np.ndarray
    radius: float

    def __post_init__(self) -> None:
        position = np.asarray(self.position, dtype=float)
        velocity = np.asarray(self.velocity, dtype=float)
        if position.shape != (2,):
            raise ValueError(f"position must have shape (2,), got {position.shape}")
        if velocity.shape != (2,):
            raise ValueError(f"velocity must have shape (2,), got {velocity.shape}")
        if not np.all(np.isfinite(position)) or not np.all(np.isfinite(velocity)):
            raise ValueError("position and velocity must be finite")
        if not np.isfinite(self.radius) or self.radius <= 0.0:
            raise ValueError(f"radius must be a positive finite number, got {self.radius}")
        # frozen dataclass: normalize through object.__setattr__
        object.__setattr__(self, "position", position)
        object.__setattr__(self, "velocity", velocity)
        object.__setattr__(self, "radius", float(self.radius))

    def center_at(self, t: float) -> np.ndarray:
        """Center position at time t (linear motion)."""
        return self.position + self.velocity * t


@dataclass(frozen=True)
class ContactResult:
    """Result of one pairwise collision query over a closed time window.

    Attributes:
        status: Classification of the outcome.
        time: Earliest contact time within the window, or None when
            status is NO_COLLISION. For ALREADY_OVERLAPPING this equals
            the window start.
        distance_at_contact: Center-to-center distance at the contact
            time (None when there is no contact).
    """

    status: ContactStatus
    time: float | None
    distance_at_contact: float | None

    @property
    def collides(self) -> bool:
        """True when contact of any kind occurs inside the window."""
        return self.status is not ContactStatus.NO_COLLISION
