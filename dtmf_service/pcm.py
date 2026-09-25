"""Local PCM file I/O (16-bit little-endian mono, raw or WAV)."""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

SUPPORTED_SAMPLE_WIDTH = 2  # 16-bit PCM


def read_pcm(path: str | Path, sample_rate: int | None = None) -> tuple[np.ndarray, int]:
    """Read a local PCM file and return (float64 samples in [-1, 1], rate).

    ``.wav`` files are parsed via the standard ``wave`` module (16-bit
    mono PCM only). Any other extension is treated as raw 16-bit
    little-endian mono PCM, in which case ``sample_rate`` is required.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"PCM input not found: {path}")
    if path.suffix.lower() == ".wav":
        return _read_wav(path)
    if sample_rate is None:
        raise ValueError("sample_rate is required for raw PCM files")
    raw = np.fromfile(path, dtype="<i2")
    return _to_float(raw), sample_rate


def _read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as wf:
        if wf.getsampwidth() != SUPPORTED_SAMPLE_WIDTH:
            raise ValueError(
                f"only 16-bit PCM WAV supported, got {wf.getsampwidth() * 8}-bit"
            )
        if wf.getnchannels() != 1:
            raise ValueError(f"only mono WAV supported, got {wf.getnchannels()} ch")
        rate = wf.getframerate()
        frames = wf.readframes(wf.getnframes())
    return _to_float(np.frombuffer(frames, dtype="<i2")), rate


def _to_float(raw: np.ndarray) -> np.ndarray:
    return raw.astype(np.float64) / 32768.0


def write_pcm(path: str | Path, samples: np.ndarray, sample_rate: int) -> None:
    """Write float samples to a 16-bit mono WAV file (test helper)."""
    clipped = np.clip(np.asarray(samples, dtype=np.float64), -1.0, 1.0)
    pcm = (clipped * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(SUPPORTED_SAMPLE_WIDTH)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm.tobytes())
