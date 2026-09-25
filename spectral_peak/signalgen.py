"""Synthetic test-signal generation."""

from __future__ import annotations

import numpy as np


def tone(
    freq_hz: float,
    amplitude: float = 1.0,
    sample_rate: float = 48000.0,
    n: int = 4096,
    phase: float = 0.0,
    dc_offset: float = 0.0,
    noise_std: float = 0.0,
    seed: int | None = None,
) -> np.ndarray:
    """A single real sinusoid; freq_hz may be a non-integer number of bins."""
    t = np.arange(n) / sample_rate
    x = dc_offset + amplitude * np.sin(2.0 * np.pi * freq_hz * t + phase)
    if noise_std > 0.0:
        rng = np.random.default_rng(seed)
        x = x + rng.normal(0.0, noise_std, size=n)
    return x


def multitone(
    components: list[tuple[float, float]],
    sample_rate: float = 48000.0,
    n: int = 4096,
    noise_std: float = 0.0,
    seed: int | None = None,
) -> np.ndarray:
    """Sum of sinusoids; each component is (freq_hz, amplitude)."""
    t = np.arange(n) / sample_rate
    x = np.zeros(n)
    for freq_hz, amplitude in components:
        x = x + amplitude * np.sin(2.0 * np.pi * freq_hz * t)
    if noise_std > 0.0:
        rng = np.random.default_rng(seed)
        x = x + rng.normal(0.0, noise_std, size=n)
    return x
