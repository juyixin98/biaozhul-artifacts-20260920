"""Synthetic signal generation and raw PCM file I/O (no audio playback).

PCM files are headerless mono ``int16`` little-endian by default; the
``dtype`` parameter accepts other integer widths (int32, uint8, ...).
Samples are returned / accepted as float in [-1, 1].
"""

from __future__ import annotations

import numpy as np


def sine(n: int, freq_fraction: float, amplitude: float = 0.9,
         phase: float = 0.0) -> np.ndarray:
    """Sinusoid; ``freq_fraction`` is cycles/sample (0.5 = Nyquist)."""
    t = np.arange(n)
    return amplitude * np.sin(2 * np.pi * freq_fraction * t + phase)


def multi_tone(n: int, components: list[tuple[float, float]]) -> np.ndarray:
    """Sum of (freq_fraction, amplitude) sinusoids."""
    t = np.arange(n)
    y = np.zeros(n, dtype=np.float64)
    for f, a in components:
        y += a * np.sin(2 * np.pi * f * t)
    return y


def chirp(n: int, f0: float = 0.0, f1: float = 0.5, amplitude: float = 0.9) -> np.ndarray:
    """Linear chirp sweeping ``f0`` -> ``f1`` (fractions of sample rate)."""
    t = np.arange(n) / n
    phase = 2 * np.pi * (f0 * np.arange(n) + 0.5 * (f1 - f0) * n * t**2)
    return amplitude * np.sin(phase)


def impulse(n: int, amplitude: float = 1.0, at: int = 0) -> np.ndarray:
    x = np.zeros(n, dtype=np.float64)
    x[at] = amplitude
    return x


def noise(n: int, amplitude: float = 0.5, seed: int = 0) -> np.ndarray:
    return amplitude * np.random.default_rng(seed).standard_normal(n)


_INT_INFO = {
    "int16": (np.int16, 32768.0),
    "int32": (np.int32, 2147483648.0),
    "uint8": (np.uint8, 128.0),
}


def read_pcm(path: str, dtype: str = "int16") -> np.ndarray:
    """Read headerless mono PCM and scale to float [-1, 1]."""
    if dtype not in _INT_INFO:
        raise ValueError(f"unsupported PCM dtype {dtype!r}; one of {sorted(_INT_INFO)}")
    np_dtype, scale = _INT_INFO[dtype]
    raw = np.fromfile(path, dtype=np.dtype(np_dtype))
    x = raw.astype(np.float64)
    if dtype == "uint8":
        x -= 128.0
    return x / scale


def write_pcm(path: str, x: np.ndarray, dtype: str = "int16") -> int:
    """Write float samples as headerless mono PCM with saturation. Returns count."""
    if dtype not in _INT_INFO:
        raise ValueError(f"unsupported PCM dtype {dtype!r}; one of {sorted(_INT_INFO)}")
    np_dtype, scale = _INT_INFO[dtype]
    info = np.iinfo(np_dtype)
    n = np.rint(np.asarray(x, dtype=np.float64) * scale)
    if dtype == "uint8":
        n += 128.0
    n = np.clip(n, info.min, info.max).astype(np_dtype)
    n.tofile(path)
    return int(np.asarray(x).size)


def write_csv(path: str, x: np.ndarray) -> int:
    """Write a 1-D float signal as one value per line."""
    np.savetxt(path, np.asarray(x, dtype=np.float64), fmt="%.9g")
    return int(np.asarray(x).size)
