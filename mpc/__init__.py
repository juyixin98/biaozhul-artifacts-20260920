"""Bounded MPC backend for a discretized double integrator.

Pure backend package: model derivation -> condensed QP -> OSQP solve ->
verified fallback. No hardware I/O.
"""

from .config import MPCConfig
from .model import DoubleIntegrator
from .mpc import MPCController, MPCResult, FallbackReason
from .disturbance import DisturbanceSpec, apply_disturbance

__all__ = [
    "MPCConfig",
    "DoubleIntegrator",
    "MPCController",
    "MPCResult",
    "FallbackReason",
    "DisturbanceSpec",
    "apply_disturbance",
]
