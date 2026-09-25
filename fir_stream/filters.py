"""FIR filter design helpers (NumPy only) and spec-based construction."""

from __future__ import annotations

import numpy as np


def identity() -> np.ndarray:
    return np.array([1.0], dtype=np.float64)


def delay(samples: int) -> np.ndarray:
    if int(samples) < 0:
        raise ValueError("delay must be >= 0")
    h = np.zeros(int(samples) + 1, dtype=np.float64)
    h[-1] = 1.0
    return h


def moving_average(num_taps: int) -> np.ndarray:
    if int(num_taps) < 1:
        raise ValueError("num_taps must be >= 1")
    return np.full(int(num_taps), 1.0 / int(num_taps), dtype=np.float64)


def lowpass(num_taps: int, cutoff_hz: float, sample_rate: float) -> np.ndarray:
    """Windowed-sinc lowpass (Hamming), gain 1 at DC."""
    if int(num_taps) < 1:
        raise ValueError("num_taps must be >= 1")
    if not 0.0 < cutoff_hz < sample_rate / 2:
        raise ValueError("cutoff_hz must be in (0, sample_rate / 2)")
    n = np.arange(int(num_taps), dtype=np.float64)
    mid = (int(num_taps) - 1) / 2.0
    fc = cutoff_hz / sample_rate  # cycles/sample
    sinc = np.sinc(2.0 * fc * (n - mid))
    window = np.hamming(int(num_taps))
    h = sinc * window
    return h / h.sum()


def highpass(num_taps: int, cutoff_hz: float, sample_rate: float) -> np.ndarray:
    """Spectral-inversion highpass built from the lowpass design."""
    lp = lowpass(num_taps, cutoff_hz, sample_rate)
    hp = -lp
    hp[(int(num_taps) - 1) // 2] += 1.0
    return hp


def from_spec(spec: dict, sample_rate: float) -> np.ndarray:
    """Build an impulse response from a JSON-friendly spec dict."""
    kind = spec.get("type")
    if kind == "identity":
        return identity()
    if kind == "delay":
        return delay(spec["samples"])
    if kind == "moving_average":
        return moving_average(spec["num_taps"])
    if kind == "lowpass":
        return lowpass(spec["num_taps"], spec["cutoff_hz"], sample_rate)
    if kind == "highpass":
        return highpass(spec["num_taps"], spec["cutoff_hz"], sample_rate)
    if kind == "custom":
        taps = np.asarray(spec["taps"], dtype=np.float64)
        if taps.ndim != 1 or taps.size == 0:
            raise ValueError("custom filter needs a non-empty 'taps' list")
        return taps
    raise ValueError(f"unknown filter type: {kind!r}")
