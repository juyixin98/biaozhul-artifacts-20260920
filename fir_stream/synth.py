"""Synthetic test-signal generation (deterministic via seed)."""

from __future__ import annotations

import numpy as np


def multi_sine(
    num_channels: int,
    duration_s: float,
    sample_rate: float,
    freqs_hz: list[float],
    seed: int = 0,
    noise_level: float = 0.01,
) -> np.ndarray:
    """Sum of sines plus light Gaussian noise, shape (channels, samples).

    Each channel gets a different phase offset so channels are not
    identical copies of one another.
    """
    rng = np.random.default_rng(seed)
    n = int(round(duration_s * sample_rate))
    t = np.arange(n, dtype=np.float64) / sample_rate
    out = np.zeros((int(num_channels), n), dtype=np.float64)
    for ch in range(int(num_channels)):
        phase = 0.37 * ch
        sig = np.zeros(n, dtype=np.float64)
        for i, f in enumerate(freqs_hz):
            sig += np.sin(2.0 * np.pi * f * t + phase * (i + 1)) / (i + 1)
        sig /= max(1, len(freqs_hz))
        sig += noise_level * rng.standard_normal(n)
        out[ch] = sig
    return out
