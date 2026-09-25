"""Analysis/synthesis window functions.

Only windows needed for STFT overlap-add are provided.  The default
``periodic=True`` window is the standard choice for spectral analysis: it is
one period of a periodic signal, i.e. a length ``N + 1`` symmetric window with
its last sample dropped.  A periodic Hann window is 0 at index 0 and almost 1
at the last index; a periodic Hamming window is non-zero at both ends.
"""

from __future__ import annotations

import numpy as np

__all__ = ["get_window", "hann", "hamming", "rect", "blackman"]


def _phase(length: int, periodic: bool) -> np.ndarray:
    if length < 1:
        raise ValueError(f"window length must be >= 1, got {length}")
    if length == 1:
        return np.zeros(1)
    denominator = length if periodic else length - 1
    return 2.0 * np.pi * np.arange(length) / denominator


def hann(length: int, periodic: bool = True) -> np.ndarray:
    """Periodic Hann window: ``0.5 - 0.5*cos(2*pi*n/N)``."""
    return 0.5 - 0.5 * np.cos(_phase(length, periodic))


def hamming(length: int, periodic: bool = True) -> np.ndarray:
    """Periodic Hamming window: ``0.54 - 0.46*cos(2*pi*n/N)``.

    Both end samples are ~0.08 (non-zero), which makes Hamming a convenient
    choice for un-padded streaming reconstruction.
    """
    return 0.54 - 0.46 * np.cos(_phase(length, periodic))


def rect(length: int, periodic: bool = True) -> np.ndarray:
    """Rectangular (boxcar) window."""
    return np.ones(length)


def blackman(length: int, periodic: bool = True) -> np.ndarray:
    """Periodic Blackman window: ``0.42 - 0.5*cos + 0.08*cos(2.)``."""
    p = _phase(length, periodic)
    return 0.42 - 0.5 * np.cos(p) + 0.08 * np.cos(2.0 * p)


_REGISTRY = {
    "hann": hann,
    "hamming": hamming,
    "rect": rect,
    "rectangular": rect,
    "blackman": blackman,
}


def get_window(name: str, length: int, periodic: bool = True) -> np.ndarray:
    """Look up a named window by case-insensitive name."""
    if not isinstance(name, str):
        raise TypeError(f"window name must be a string, got {type(name)!r}")
    try:
        factory = _REGISTRY[name.lower()]
    except KeyError as exc:
        known = ", ".join(sorted(set(_REGISTRY)))
        raise ValueError(f"unknown window {name!r}; expected one of: {known}") from exc
    return factory(length, periodic=periodic)
