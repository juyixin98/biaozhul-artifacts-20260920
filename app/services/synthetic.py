"""Deterministic synthetic input sequences for demos and reproducible tests.

Every generator returns a fresh ``float64`` array; an integer seed makes
randomised scenarios reproducible.  The scenarios deliberately stress the
three failure modes that distinguish summation rules:

* large-number cancellation (``big_cancel``),
* accumulation of values below the running sum's resolution (``eps_tail``),
* many small terms after a large offset (``harmonic_shuffled`` style).
"""
from __future__ import annotations

import math

import numpy as np

from ..config import MACHINE_EPSILON

U = MACHINE_EPSILON


def repeating_decimal(n: int, x: float = 0.1) -> np.ndarray:
    """``n`` copies of a value not exactly representable in binary."""
    return np.full(n, float(x), dtype=np.float64)


def big_cancel(n_small: int, magnitude: float = 1e16, small: float = 1.0) -> np.ndarray:
    """``[M, 1, 1, ..., 1, -M]``: small terms sit below M's resolution.

    Exact result is ``n_small * small``; naive summation loses every small
    term once ``small < U * M``, while Kahan retains them.
    """
    m = float(magnitude)
    ones = np.full(n_small, float(small), dtype=np.float64)
    return np.concatenate(([m], ones, [-m]))


def eps_tail(n_tiny: int, anchor: float = 1.0) -> np.ndarray:
    """``[1, u, u, ...]`` with ``u = 2^-53``.

    Each ``u`` is exactly half the spacing of floats near 1.0, so naive
    summation rounds every one of them away; the exact total rounds to 1.0
    too when ``n_tiny`` is even, and to ``1 + 2u`` ... i.e. nextafter once,
    when odd.
    """
    tiny = np.full(n_tiny, U, dtype=np.float64)
    return np.concatenate(([float(anchor)], tiny))


def harmonic(n: int) -> np.ndarray:
    """First ``n`` terms of the alternating-free harmonic sequence, 1/k."""
    k = np.arange(1, n + 1, dtype=np.float64)
    return 1.0 / k


def alternating(n: int) -> np.ndarray:
    """Alternating harmonic series ``(-1)**(k+1)/k`` (severe cancellation)."""
    k = np.arange(1, n + 1, dtype=np.float64)
    signs = np.where(np.arange(n) % 2 == 0, 1.0, -1.0)
    return signs / k


def signed_zeros(neg: int, pos: int) -> np.ndarray:
    return np.array([-0.0] * neg + [0.0] * pos, dtype=np.float64)


def mixed_scales(n: int, seed: int = 0) -> np.ndarray:
    """Random values whose magnitudes span ~24 decimal orders."""
    rng = np.random.default_rng(seed)
    exponents = rng.integers(-12, 12, size=n)
    return (rng.choice([-1.0, 1.0], size=n) * np.power(10.0, exponents)).astype(np.float64)


SCENARIOS = {
    "repeating_decimal": lambda n: repeating_decimal(n),
    "big_cancel": lambda n: big_cancel(max(0, n - 2)),
    "eps_tail": lambda n: eps_tail(max(0, n - 1)),
    "harmonic": harmonic,
    "alternating": alternating,
    "mixed_scales": lambda n: mixed_scales(n, seed=0),
}


def build_scenario(name: str, n: int) -> np.ndarray:
    if name not in SCENARIOS:
        raise KeyError(name)
    if n <= 0:
        raise ValueError("n must be positive")
    return np.asarray(SCENARIOS[name](n), dtype=np.float64)
