"""Synthetic dual-tone signal generation (test/acceptance input only).

This module exists so the service can be exercised end-to-end without
real recordings. It generates the classic 4x4 DTMF keypad tones plus
additive white Gaussian noise at a requested SNR.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

LOW_FREQS: tuple[float, ...] = (697.0, 770.0, 852.0, 941.0)
HIGH_FREQS: tuple[float, ...] = (1209.0, 1336.0, 1477.0, 1633.0)

# key -> (low_freq, high_freq)
KEYPAD: dict[str, tuple[float, float]] = {}
for _row, _low in enumerate(LOW_FREQS):
    for _col, _high in enumerate(HIGH_FREQS):
        KEYPAD["123A456B789C*0#D"[_row * 4 + _col]] = (_low, _high)

ALL_KEYS: tuple[str, ...] = tuple(KEYPAD.keys())


@dataclass(frozen=True)
class ToneSpec:
    """One dual-tone segment."""

    key: str
    duration_ms: float = 100.0
    amplitude: float = 0.5
    twist_db: float = 0.0  # high-tone level relative to low-tone level
    freq_offset_pct: float = 0.0  # relative frequency error applied to both tones


def tone_freqs(key: str) -> tuple[float, float]:
    """Return (low, high) nominal frequencies for a keypad key."""
    try:
        return KEYPAD[key.upper()]
    except KeyError:
        raise ValueError(f"unknown key {key!r}; valid keys: {sorted(KEYPAD)}") from None


def synthesize_tone(
    key: str,
    sample_rate: int,
    duration_ms: float = 100.0,
    amplitude: float = 0.5,
    twist_db: float = 0.0,
    freq_offset_pct: float = 0.0,
) -> np.ndarray:
    """Synthesize one dual-tone segment as float64 samples in [-1, 1]."""
    low, high = tone_freqs(key)
    scale = 1.0 + freq_offset_pct / 100.0
    n = max(1, int(round(sample_rate * duration_ms / 1000.0)))
    t = np.arange(n, dtype=np.float64) / sample_rate
    high_amp = amplitude * 10.0 ** (twist_db / 20.0)
    signal = amplitude * np.sin(2.0 * np.pi * low * scale * t) + high_amp * np.sin(
        2.0 * np.pi * high * scale * t
    )
    peak = np.max(np.abs(signal))
    if peak > 1.0:
        signal = signal / peak
    return signal


def add_noise(
    signal: np.ndarray, snr_db: float | None, rng: np.random.Generator
) -> np.ndarray:
    """Add white Gaussian noise to reach the requested SNR (dB).

    ``snr_db=None`` means no noise. SNR is measured against the mean
    signal power over the whole array.
    """
    if snr_db is None:
        return signal
    signal_power = float(np.mean(signal**2))
    if signal_power == 0.0:
        # Silence: define noise relative to full-scale so tests stay sane.
        noise_power = 10.0 ** (-snr_db / 10.0) * 0.5**2
    else:
        noise_power = signal_power / (10.0 ** (snr_db / 10.0))
    noise = rng.normal(0.0, np.sqrt(noise_power), size=signal.shape)
    return signal + noise


def synthesize_sequence(
    keys: str,
    sample_rate: int,
    tone_ms: float = 100.0,
    gap_ms: float = 50.0,
    amplitude: float = 0.5,
    twist_db: float = 0.0,
    freq_offset_pct: float = 0.0,
    snr_db: float | None = None,
    seed: int | None = None,
) -> np.ndarray:
    """Synthesize a digit sequence with silence gaps and optional noise."""
    rng = np.random.default_rng(seed)
    gap = np.zeros(int(round(sample_rate * gap_ms / 1000.0)), dtype=np.float64)
    parts: list[np.ndarray] = [gap]
    for key in keys:
        parts.append(
            synthesize_tone(
                key,
                sample_rate,
                duration_ms=tone_ms,
                amplitude=amplitude,
                twist_db=twist_db,
                freq_offset_pct=freq_offset_pct,
            )
        )
        parts.append(gap)
    signal = np.concatenate(parts)
    return add_noise(signal, snr_db, rng)
