"""Goertzel algorithm: single-frequency power estimation.

The Goertzel filter computes one bin of the DFT in O(N) multiply-adds,
which is cheaper than an FFT when only a handful of target frequencies
are needed (8 for DTMF). We evaluate at the *exact* target frequency
(generalized Goertzel), so no bin-rounding error is introduced.
"""

from __future__ import annotations

import numpy as np


def goertzel_power(samples: np.ndarray, sample_rate: float, target_freq: float) -> float:
    """Return the signal power concentrated at ``target_freq``.

    For a pure sine of amplitude A and frequency ``target_freq`` the
    returned value is approximately ``(A * N / 2) ** 2`` where N is the
    number of samples. Use :func:`tone_mean_square` to convert to a
    mean-square (energy-per-sample) value comparable with ``mean(x**2)``.

    Parameters
    ----------
    samples:
        1-D array of audio samples (float or int dtype).
    sample_rate:
        Sampling rate in Hz.
    target_freq:
        Frequency to probe, in Hz. Must be in (0, sample_rate / 2).

    Raises
    ------
    ValueError
        If the input is empty or the frequency is out of range.
    """
    x = np.asarray(samples, dtype=np.float64).ravel()
    n = x.size
    if n == 0:
        raise ValueError("goertzel_power: empty input")
    if not 0.0 < target_freq < sample_rate / 2.0:
        raise ValueError(
            f"target_freq {target_freq} out of range (0, {sample_rate / 2.0})"
        )

    omega = 2.0 * np.pi * target_freq / sample_rate
    coeff = 2.0 * np.cos(omega)

    s_prev = 0.0
    s_prev2 = 0.0
    for sample in x:
        s = sample + coeff * s_prev - s_prev2
        s_prev2 = s_prev
        s_prev = s

    # Exact-frequency Goertzel: combine the two state variables with the
    # complex exponential to get the DFT value at target_freq.
    real = s_prev - s_prev2 * np.cos(omega)
    imag = s_prev2 * np.sin(omega)
    return real * real + imag * imag


def tone_mean_square(power: float, n_samples: int, window_sum: float | None = None) -> float:
    """Convert Goertzel power to estimated tone mean-square amplitude.

    For a pure tone of amplitude A, ``mean(x**2) == A**2 / 2`` and the
    Goertzel power is ``(A*S/2)**2`` where S is the window sum (N for a
    rectangular window), hence ``A**2/2 == 2*power / S**2``.
    """
    if n_samples <= 0:
        raise ValueError("n_samples must be positive")
    s = float(window_sum) if window_sum is not None else float(n_samples)
    return 2.0 * power / (s * s)
