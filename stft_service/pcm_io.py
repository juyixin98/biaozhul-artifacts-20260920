"""Raw PCM file I/O.

Header-less linear PCM only (no WAV container): little-endian interleaved
samples, mono. Supported sample formats:

``s16`` signed 16-bit, ``s32`` signed 32-bit, ``f32`` IEEE float32,
``f64`` IEEE float64. Integer samples are scaled to/from the [-1, 1) range.
"""

from __future__ import annotations

import numpy as np

_FORMATS = {
    "s16": ("<i2", 32768.0),
    "s32": ("<i4", 2147483648.0),
    "f32": ("<f4", 1.0),
    "f64": ("<f8", 1.0),
}

FORMAT_NAMES = tuple(_FORMATS)


def read_pcm(path: str, sample_format: str = "s16") -> np.ndarray:
    """Read a mono raw-PCM file into a float64 numpy array."""
    if sample_format not in _FORMATS:
        raise ValueError(
            f"unsupported sample_format {sample_format!r}; "
            f"expected one of {FORMAT_NAMES}"
        )
    dtype, scale = _FORMATS[sample_format]
    raw = np.fromfile(path, dtype=np.dtype(dtype))
    x = raw.astype(np.float64)
    if scale != 1.0:
        x /= scale
    return x


def write_pcm(path: str, x: np.ndarray, sample_format: str = "s16") -> int:
    """Write a mono float64 array as raw PCM; returns number of samples.

    Values are clipped to [-1, 1] before integer conversion to avoid wrap.
    """
    if sample_format not in _FORMATS:
        raise ValueError(
            f"unsupported sample_format {sample_format!r}; "
            f"expected one of {FORMAT_NAMES}"
        )
    dtype, scale = _FORMATS[sample_format]
    x = np.asarray(x, dtype=np.float64)
    if x.ndim != 1:
        raise ValueError(f"signal must be 1-D, got shape {x.shape}")
    if scale != 1.0:
        q = np.clip(x, -1.0, 1.0) * scale
        # round-then-truncate avoids the 1-LSB bias of astype truncation
        out = np.rint(q).astype(dtype)
    else:
        out = x.astype(dtype)
    out.tofile(path)
    return int(out.size)
