"""Window functions and their coherent gain.

Coherent gain CG = mean(w). For a real sinusoid of amplitude A, the FFT
magnitude at the exact peak bin is A * sum(w) / 2, so the amplitude
correction factor is 2 / sum(w).
"""

from __future__ import annotations

import numpy as np

WINDOWS = ("hann", "rect", "blackmanharris")


def get_window(name: str, n: int) -> np.ndarray:
    """Return a periodic window of length n (DFT-even, standard for FFT work)."""
    if n <= 0:
        raise ValueError("window length must be positive")
    if name == "hann":
        return 0.5 - 0.5 * np.cos(2.0 * np.pi * np.arange(n) / n)
    if name == "rect":
        return np.ones(n)
    if name == "blackmanharris":
        k = np.arange(n)
        x = 2.0 * np.pi * k / n
        return (
            0.35875
            - 0.48829 * np.cos(x)
            + 0.14128 * np.cos(2.0 * x)
            - 0.01168 * np.cos(3.0 * x)
        )
    raise ValueError(f"unknown window {name!r}; choose from {WINDOWS}")


def mainlobe_width_bins(name: str) -> int:
    """Full mainlobe width in bins; tones closer than this share a mainlobe."""
    return {"hann": 4, "rect": 2, "blackmanharris": 8}[name]
