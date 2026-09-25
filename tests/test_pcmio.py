"""Tests for raw PCM and .npy file IO."""

from __future__ import annotations

import numpy as np
import pytest

from streaming_stft.pcmio import (
    PCMSource,
    read_pcm,
    read_signal_file,
    write_pcm,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "dtype", ["float64", "float32", "int32", "int16", "int8", "uint8"]
)
def test_pcm_roundtrip(tmp_path, dtype: str) -> None:
    x = np.linspace(-0.9, 0.9, 100)
    path = tmp_path / "sig.pcm"
    write_pcm(path, x, dtype=dtype)
    y = read_pcm(PCMSource(str(path), dtype=dtype))
    # Low-bit formats lose precision; float formats are exact.
    tol = 0.0 if dtype.startswith("float64") else 2 ** (
        -{"float32": 23, "int32": 30, "int16": 14, "int8": 6, "uint8": 6}[dtype]
    )
    assert np.max(np.abs(y - x)) <= tol + 1e-12


def test_write_pcm_clips_without_wrapping(tmp_path) -> None:
    path = tmp_path / "clip.pcm"
    write_pcm(path, np.array([2.0, -2.0]), dtype="int16")
    y = read_pcm(PCMSource(str(path), dtype="int16"))
    assert y[0] == pytest.approx(1.0, abs=1e-4)
    assert y[1] <= -0.9999


def test_misaligned_file_size_raises(tmp_path) -> None:
    path = tmp_path / "bad.pcm"
    path.write_bytes(b"\x00\x00\x00")
    with pytest.raises(ValueError, match="not a multiple"):
        read_pcm(PCMSource(str(path), dtype="int16"))


def test_unknown_dtype_raises(tmp_path) -> None:
    path = tmp_path / "x.pcm"
    write_pcm(path, np.ones(2))
    with pytest.raises(ValueError, match="unsupported PCM dtype"):
        read_pcm(PCMSource(str(path), dtype="int24"))


def test_npy_roundtrip(tmp_path) -> None:
    x = np.arange(10, dtype=np.float32)
    path = tmp_path / "sig.npy"
    np.save(path, x)
    y = read_signal_file(path)
    assert y.dtype == np.float64
    assert np.array_equal(y, x.astype(np.float64))


def test_npy_rejects_multidim(tmp_path) -> None:
    path = tmp_path / "two_d.npy"
    np.save(path, np.zeros((3, 3)))
    with pytest.raises(ValueError, match="1-D"):
        read_signal_file(path)


def test_missing_file_raises() -> None:
    with pytest.raises(FileNotFoundError):
        read_signal_file("/nonexistent/file.pcm")
