"""Deterministic synthetic disturbance generator.

No randomness, no time-of-day, no hardware.  The same ``(step, seed,
amplitude)`` always produces the same external acceleration on every machine
and every Python version (pure integer-step trigonometry through ``math``)::

    d_k = amplitude / 2 * (sin(w1*k + phi) + cos(w2*k + phi))

The signal is a bounded quasi-periodic acceleration: |d_k| <= amplitude
(each term is in [-1, 1], so their sum is in [-2, 2] and the factor 1/2
enforces the hard bound), combining two incommensurate frequencies so it
does not repeat over short horizons.
"""

from __future__ import annotations

import math


def deterministic_acceleration(step: int, amplitude: float, seed: int = 0) -> float:
    """Return the deterministic external acceleration at integer ``step``.

    Guarantees |d_k| <= ``amplitude`` exactly: the two independent-phase
    sinusoids each lie in [-1, 1] (sum in [-2, 2]) and are scaled by 1/2.
    ``seed`` must be a non-negative integer and only shifts/rescales the
    fixed frequencies/phase.
    """
    if amplitude < 0:
        raise ValueError("disturbance amplitude must be non-negative")
    if seed < 0:
        raise ValueError("disturbance seed must be a non-negative integer")
    k = int(step)
    s = seed + 1
    w1 = 0.37 * s
    w2 = 0.61 * s
    phi = 0.131 * s
    return float(
        amplitude
        / 2.0
        * (math.sin(w1 * k + phi) + math.cos(w2 * k + phi))
    )
