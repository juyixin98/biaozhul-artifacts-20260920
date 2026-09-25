"""Synthetic test-signal generation."""

from __future__ import annotations

import numpy as np


def synthesize(
    length: int,
    sample_rate: float = 8000.0,
    frequencies=(440.0, 1000.0),
    amplitudes=(0.6, 0.3),
    seed: int = 1234,
    noise_std: float = 0.01,
) -> np.ndarray:
    """Deterministic multi-tone + weak Gaussian noise signal, float64 in [-1, 1].

    A fixed ``seed`` makes every run / report reproducible. The noise
    component verifies reconstruction is not limited to pure tones.
    """
    if length < 0:
        raise ValueError("length must be non-negative")
    length = int(length)
    t = np.arange(length, dtype=np.float64) / float(sample_rate)
    x = np.zeros(length, dtype=np.float64)
    for f, a in zip(frequencies, amplitudes):
        x += float(a) * np.sin(2.0 * np.pi * float(f) * t)
    if noise_std and length:
        rng = np.random.default_rng(seed)
        x += float(noise_std) * rng.standard_normal(length)
    peak = float(np.max(np.abs(x))) if length else 0.0
    if peak > 1.0:
        x /= peak
    return x
