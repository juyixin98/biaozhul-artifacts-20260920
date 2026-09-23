"""Deterministic synthetic disturbances for closed-loop simulation.

No hardware, no randomness: the disturbance at step k is a fully specified
function of k, which makes every simulation reproducible.

Supported kinds (per state component p and v independently, applied
additively to x_{k+1} = A_d x_k + B_d u_k + w_k):

* "none":       w = 0
* "constant":   w_k = amplitude
* "sine":       w_k = amplitude * sin(2*pi*frequency*k*dt + phase)
* "square":     amplitude * sign(sin(...))
* "ramp":       amplitude * k
* "bump":       amplitude only at step k == step (0 elsewhere)

Amplitude can be a scalar (applied to velocity component only) or a
2-vector [w_position, w_velocity].
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class DisturbanceSpec:
    kind: str = "none"
    amplitude: tuple[float, float] | float = 0.0
    frequency: float = 1.0   # Hz, for sine/square
    phase: float = 0.0       # radians
    step: int = 0            # bump location

    def vector(self) -> tuple[float, float]:
        a = self.amplitude
        if isinstance(a, (int, float)):
            return (0.0, float(a))
        if len(a) != 2:
            raise ValueError("amplitude must be scalar or length-2")
        return (float(a[0]), float(a[1]))


def apply_disturbance(spec: DisturbanceSpec, k: int, dt: float) -> tuple[float, float]:
    """Return w_k = (position disturbance, velocity disturbance)."""
    ax, av = spec.vector()
    kind = spec.kind.lower()

    if kind == "none":
        return (0.0, 0.0)
    if kind == "constant":
        return (ax, av)
    if kind == "sine":
        s = math.sin(2.0 * math.pi * spec.frequency * k * dt + spec.phase)
        return (ax * s, av * s)
    if kind == "square":
        s = math.sin(2.0 * math.pi * spec.frequency * k * dt + spec.phase)
        s = 1.0 if s >= 0 else -1.0
        return (ax * s, av * s)
    if kind == "ramp":
        return (ax * k, av * k)
    if kind == "bump":
        return (ax, av) if k == spec.step else (0.0, 0.0)
    raise ValueError(f"unknown disturbance kind: {spec.kind!r}")
