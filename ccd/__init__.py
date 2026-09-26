"""Continuous collision detection (CCD) for 2D circles on linear trajectories.

Pure offline computation library: given a circular robot and circular
obstacles moving at constant velocity, compute the exact earliest time of
contact by solving a quadratic equation in closed form. No discrete
sampling is used anywhere in the solver.
"""

from ccd.models import CircleBody, ContactResult, ContactStatus
from ccd.solver import earliest_contact, quadratic_coefficients

__all__ = [
    "CircleBody",
    "ContactResult",
    "ContactStatus",
    "earliest_contact",
    "quadratic_coefficients",
]

__version__ = "0.1.0"
