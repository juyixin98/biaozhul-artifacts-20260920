"""Analysis / synthesis window construction.

All windows are generated in the *periodic* convention (length ``n_fft``),
which is the convention used with DFT-based STFT: the window contains exactly
one period and the second endpoint (which would duplicate the first) is
omitted. The periodic Hann window satisfies COLA at 50 % overlap.
"""

from __future__ import annotations

import numpy as np

WINDOW_NAMES = ("hann", "hamming", "blackman", "rect")


def make_window(name: str, n_fft: int) -> np.ndarray:
    """Return a periodic window of length ``n_fft``.

    Parameters
    ----------
    name:
        One of :data:`WINDOW_NAMES` (``"hann"``, ``""hamming"``,
        ``"blackman"``, ``"rect"``).
    n_fft:
        Window length (also the DFT length). Must be >= 1.
    """
    if not isinstance(n_fft, (int, np.integer)) or n_fft < 1:
        raise ValueError(f"n_fft must be a positive integer, got {n_fft!r}")
    n_fft = int(n_fft)

    key = name.lower()
    if key == "rect":
        return np.ones(n_fft, dtype=np.float64)
    if key == "hann":
        # periodic Hann: n+1 denominator, first n samples
        n = np.arange(n_fft, dtype=np.float64)
        return 0.5 - 0.5 * np.cos(2.0 * np.pi * n / n_fft)
    if key == "hamming":
        # 0.54/0.46 periodic (n+1 denominator convention)
        n = np.arange(n_fft, dtype=np.float64)
        return 0.54 - 0.46 * np.cos(2.0 * np.pi * n / n_fft)
    if key == "blackman":
        n = np.arange(n_fft, dtype=np.float64)
        a0, a1, a2 = 0.42, 0.5, 0.08
        return (
            a0
            - a1 * np.cos(2.0 * np.pi * n / n_fft)
            + a2 * np.cos(4.0 * np.pi * n / n_fft)
        )
    raise ValueError(f"unknown window {name!r}; expected one of {WINDOW_NAMES}")
