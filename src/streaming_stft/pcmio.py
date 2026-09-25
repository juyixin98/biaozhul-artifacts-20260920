"""Raw PCM file input/output.

Only header-less single-channel PCM is handled (the service is a back-end;
no container formats).  Integer samples are scaled to the ``[-1, 1]`` float
range on read and rescaled + clipped on write.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

__all__ = ["PCM_DTYPES", "PCMSource", "read_pcm", "write_pcm", "read_signal_file"]

# dtype description -> (numpy dtype, normalized scale divisor)
PCM_DTYPES: dict[str, tuple[str, float]] = {
    "float64": ("<f8", 1.0),
    "float32": ("<f4", 1.0),
    "int32": ("<i4", float(2**31)),
    "int16": ("<i2", float(2**15)),
    "int8": ("<i1", float(2**7)),
    "uint8": ("<u1", 128.0),
}


@dataclass(frozen=True)
class PCMSource:
    """Description of a raw PCM file."""

    path: str
    dtype: str = "float64"

    def sample_count(self) -> int:
        itemsize = int(np.dtype(PCM_DTYPES[self.dtype][0]).itemsize)
        size = Path(self.path).stat().st_size
        if size % itemsize != 0:
            raise ValueError(
                f"file size {size} is not a multiple of {itemsize} bytes "
                f"for dtype {self.dtype!r}; is the PCM format correct?"
            )
        return size // itemsize


def read_pcm(source: PCMSource) -> np.ndarray:
    """Read a mono raw PCM file into a float64 array."""
    if source.dtype not in PCM_DTYPES:
        raise ValueError(
            f"unsupported PCM dtype {source.dtype!r}; "
            f"expected one of {sorted(PCM_DTYPES)}"
        )
    np_dtype, scale = PCM_DTYPES[source.dtype]
    raw = np.fromfile(source.path, dtype=np_dtype)
    itemsize = int(np.dtype(np_dtype).itemsize)
    size = Path(source.path).stat().st_size
    if size % itemsize != 0:
        raise ValueError(
            f"file size {size} is not a multiple of {itemsize} bytes "
            f"for dtype {source.dtype!r}; is the PCM format correct?"
        )
    if raw.ndim != 1:  # pragma: no cover - fromfile is always 1-D
        raise ValueError("PCM input must be mono (1-D)")
    signal = raw.astype(np.float64)
    if source.dtype == "uint8":
        signal = (signal - 128.0) / 128.0
    elif scale != 1.0:
        signal = signal / scale
    return signal


def write_pcm(
    path: str | Path, signal: np.ndarray, dtype: str = "float64"
) -> None:
    """Write a mono float signal as raw little-endian PCM (clipped/scaled)."""
    if dtype not in PCM_DTYPES:
        raise ValueError(f"unsupported PCM dtype {dtype!r}")
    np_dtype, scale = PCM_DTYPES[dtype]
    x = np.asarray(signal, dtype=np.float64).reshape(-1)
    if dtype == "uint8":
        q = np.clip(np.rint(x * 128.0 + 128.0), 0, 255)
    elif scale != 1.0:
        q = np.clip(np.rint(x * scale), -scale, scale - 1.0)
    else:
        q = x
    q.astype(np.dtype(np_dtype)).tofile(path)


def read_signal_file(path: str | Path, dtype: str = "float64") -> np.ndarray:
    """Read ``.npy`` or raw ``.pcm``/other files.

    ``.npy`` files are loaded directly (must contain a 1-D array); everything
    else is treated as header-less PCM of the given dtype.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(p)
    if p.suffix.lower() == ".npy":
        signal = np.load(p)
        if signal.ndim != 1:
            raise ValueError(f".npy signal must be 1-D, got shape {signal.shape}")
        return signal.astype(np.float64)
    return read_pcm(PCMSource(str(p), dtype=dtype))
