"""PCM file I/O and synthetic test-signal generation.

Supported raw PCM encodings (no headers, mono):

* ``s16le`` — signed 16-bit little-endian, full scale +/-32768
* ``f32le`` — 32-bit float little-endian, nominal full scale +/-1.0
* ``f64le`` — 64-bit float little-endian, nominal full scale +/-1.0
"""

from __future__ import annotations

import numpy as np

_DTYPES = {
    "s16le": (np.dtype("<i2"), 32768.0),
    "f32le": (np.dtype("<f4"), 1.0),
    "f64le": (np.dtype("<f8"), 1.0),
}

ENCODINGS = tuple(_DTYPES)


def read_pcm(path: str, encoding: str = "s16le") -> np.ndarray:
    """Read a raw PCM file and return float64 samples normalized to ~+/-1."""
    dtype, scale = _codec(encoding)
    data = np.fromfile(path, dtype=dtype)
    return data.astype(np.float64) / scale


def write_pcm(path: str, samples: np.ndarray, encoding: str = "s16le") -> None:
    """Write float samples to a raw PCM file (int16 is clipped, not wrapped)."""
    dtype, scale = _codec(encoding)
    x = np.asarray(samples, dtype=np.float64)
    if np.issubdtype(dtype, np.integer):
        x = np.clip(x * scale, -scale, scale - 1.0)
    x.astype(dtype).tofile(path)


def _codec(encoding: str) -> tuple[np.dtype, float]:
    try:
        return _DTYPES[encoding]
    except KeyError:
        raise ValueError(f"unsupported encoding {encoding!r}; choose from {ENCODINGS}") from None


# ----------------------------------------------------------------------
# synthetic signals
# ----------------------------------------------------------------------
def biased_sine(
    n_samples: int,
    sample_rate: float,
    freq_hz: float = 440.0,
    amplitude: float = 0.5,
    bias: float = 0.3,
) -> np.ndarray:
    """Sine wave with a constant DC bias."""
    t = np.arange(n_samples, dtype=np.float64) / sample_rate
    return bias + amplitude * np.sin(2.0 * np.pi * freq_hz * t)


def bias_step(
    n_samples: int,
    step_index: int,
    bias_before: float = 0.2,
    bias_after: float = -0.4,
) -> np.ndarray:
    """Constant signal whose DC bias jumps at ``step_index``."""
    if not 0 <= step_index <= n_samples:
        raise ValueError("step_index must be within [0, n_samples]")
    x = np.full(n_samples, bias_before, dtype=np.float64)
    x[step_index:] = bias_after
    return x
