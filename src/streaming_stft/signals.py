"""Deterministic synthetic test signals.

All generators return a float64 1-D array sampled at ``sample_rate`` Hz.
A fixed NumPy ``Generator`` seed makes every request reproducible.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

__all__ = ["SyntheticSpec", "generate_signal", "time_axis"]


@dataclass(frozen=True)
class SyntheticSpec:
    """Parameters of a deterministic synthetic signal.

    Attributes
    ----------
    duration_s:
        Length in seconds (must be > 0).
    sample_rate:
        Sampling rate in Hz.
    frequencies:
        Sine components in Hz, summed with equal amplitude.
    amplitudes:
        Optional per-component amplitudes; defaults to all ones.
    noise_std:
        Standard deviation of additive Gaussian noise (0 disables it).
    chirp_to:
        Optional end frequency of an added linear chirp in Hz.
    seed:
        Random generator seed for the noise component.
    """

    duration_s: float = 1.0
    sample_rate: int = 8000
    frequencies: tuple[float, ...] = (440.0,)
    amplitudes: tuple[float, ...] | None = None
    noise_std: float = 0.0
    chirp_to: float | None = None
    seed: int = 0

    def n_samples(self) -> int:
        return int(round(self.duration_s * self.sample_rate))


def time_axis(n_samples: int, sample_rate: int) -> np.ndarray:
    return np.arange(n_samples, dtype=np.float64) / float(sample_rate)


def generate_signal(spec: SyntheticSpec) -> np.ndarray:
    """Build the signal: sines, optional linear chirp, optional noise."""
    if spec.duration_s <= 0:
        raise ValueError(f"duration_s must be > 0, got {spec.duration_s}")
    if spec.sample_rate <= 0:
        raise ValueError(f"sample_rate must be > 0, got {spec.sample_rate}")
    n = spec.n_samples()
    t = time_axis(n, spec.sample_rate)
    signal = np.zeros(n, dtype=np.float64)

    amplitudes = spec.amplitudes
    if amplitudes is None:
        amplitudes = (1.0,) * len(spec.frequencies)
    if len(amplitudes) != len(spec.frequencies):
        raise ValueError(
            f"got {len(spec.frequencies)} frequencies but "
            f"{len(amplitudes)} amplitudes"
        )
    for frequency, amplitude in zip(spec.frequencies, amplitudes):
        signal += amplitude * np.sin(2.0 * np.pi * frequency * t)

    if spec.chirp_to is not None and spec.frequencies:
        f0 = float(spec.frequencies[0])
        # Linear instantaneous frequency from f0 to chirp_to.
        phase = 2.0 * np.pi * (
            f0 * t + 0.5 * (spec.chirp_to - f0) / spec.duration_s * t * t
        )
        signal += 0.5 * np.sin(phase)

    if spec.noise_std > 0.0:
        rng = np.random.default_rng(spec.seed)
        signal += rng.normal(scale=spec.noise_std, size=n)
    return signal
