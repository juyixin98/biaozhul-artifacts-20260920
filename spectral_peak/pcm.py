"""Reading raw (headerless) PCM files into float samples."""

from __future__ import annotations

import numpy as np

DTYPES = {
    "s16le": np.dtype("<i2"),
    "s24le": np.dtype("<i4"),  # stored left-justified; see read_pcm note
    "s32le": np.dtype("<i4"),
    "f32le": np.dtype("<f4"),
    "f64le": np.dtype("<f8"),
}

_INT_FULL_SCALE = {
    "s16le": float(1 << 15),
    "s24le": float(1 << 31),  # left-justified 24-bit in 32-bit container
    "s32le": float(1 << 31),
}


def read_pcm(
    path: str,
    fmt: str = "s16le",
    channels: int = 1,
    channel: int = 0,
) -> np.ndarray:
    """Read a raw PCM file and return mono float64 samples in [-1, 1].

    fmt: one of s16le / s24le / s32le / f32le / f64le. For s24le the file
    must store each sample left-justified in a 32-bit little-endian word
    (packed 3-byte s24le is not supported).
    """
    if fmt not in DTYPES:
        raise ValueError(f"unsupported fmt {fmt!r}; choose from {sorted(DTYPES)}")
    if channels < 1:
        raise ValueError("channels must be >= 1")
    if not 0 <= channel < channels:
        raise ValueError(f"channel {channel} out of range for {channels} channels")

    raw = np.fromfile(path, dtype=DTYPES[fmt])
    if raw.size == 0:
        raise ValueError(f"{path}: no samples read")
    if raw.size % channels != 0:
        raise ValueError(
            f"{path}: {raw.size} samples is not a multiple of {channels} channels"
        )
    mono = raw.reshape(-1, channels)[:, channel].astype(np.float64)
    scale = _INT_FULL_SCALE.get(fmt)
    if scale is not None:
        mono = mono / scale
    return mono
