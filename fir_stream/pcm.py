"""Raw (headerless) PCM I/O.  Interleaved on disk, (channels, samples)
float64 in memory."""

from __future__ import annotations

import numpy as np

_FORMATS = {
    "s16le": np.dtype("<i2"),
    "f32le": np.dtype("<f4"),
    "f64le": np.dtype("<f8"),
}


def read_pcm(path: str, fmt: str, num_channels: int) -> np.ndarray:
    if fmt not in _FORMATS:
        raise ValueError(f"unsupported PCM format: {fmt!r}")
    raw = np.fromfile(path, dtype=_FORMATS[fmt])
    if raw.size % num_channels != 0:
        raise ValueError(
            f"file holds {raw.size} samples, not a multiple of {num_channels} channels"
        )
    interleaved = raw.astype(np.float64)
    if fmt == "s16le":
        interleaved /= 32768.0
    return interleaved.reshape(-1, num_channels).T.copy()


def write_pcm(path: str, data: np.ndarray, fmt: str) -> None:
    if fmt not in _FORMATS:
        raise ValueError(f"unsupported PCM format: {fmt!r}")
    data = np.asarray(data, dtype=np.float64)
    if data.ndim != 2:
        raise ValueError("data must be (channels, samples)")
    interleaved = data.T.reshape(-1)
    if fmt == "s16le":
        interleaved = np.clip(interleaved, -1.0, 32767.0 / 32768.0)
        interleaved = np.round(interleaved * 32768.0)
    interleaved.astype(_FORMATS[fmt]).tofile(path)
