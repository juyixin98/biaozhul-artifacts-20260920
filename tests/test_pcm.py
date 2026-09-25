"""PCM reader tests."""

import numpy as np
import pytest

from spectral_peak.pcm import read_pcm


def test_s16le_roundtrip(tmp_path):
    samples = np.array([0, 16384, -16384, 32767, -32768], dtype="<i2")
    path = tmp_path / "a.pcm"
    samples.tofile(path)
    out = read_pcm(str(path), fmt="s16le")
    assert out.dtype == np.float64
    assert out[1] == pytest.approx(0.5)
    assert out[2] == pytest.approx(-0.5)
    assert out[3] == pytest.approx(32767 / 32768)
    assert out[4] == pytest.approx(-1.0)


def test_channel_selection(tmp_path):
    left = np.full(8, 16384, dtype="<i2")
    right = np.full(8, -16384, dtype="<i2")
    interleaved = np.empty(16, dtype="<i2")
    interleaved[0::2] = left
    interleaved[1::2] = right
    path = tmp_path / "stereo.pcm"
    interleaved.tofile(path)
    assert read_pcm(str(path), channels=2, channel=0)[0] == pytest.approx(0.5)
    assert read_pcm(str(path), channels=2, channel=1)[0] == pytest.approx(-0.5)


def test_f32le_passthrough(tmp_path):
    samples = np.array([0.0, 0.25, -0.25], dtype="<f4")
    path = tmp_path / "f.pcm"
    samples.tofile(path)
    out = read_pcm(str(path), fmt="f32le")
    assert out[1] == pytest.approx(0.25)


def test_bad_channel_rejected(tmp_path):
    np.zeros(4, dtype="<i2").tofile(tmp_path / "x.pcm")
    with pytest.raises(ValueError):
        read_pcm(str(tmp_path / "x.pcm"), channels=2, channel=2)


def test_misaligned_file_rejected(tmp_path):
    np.zeros(5, dtype="<i2").tofile(tmp_path / "odd.pcm")
    with pytest.raises(ValueError):
        read_pcm(str(tmp_path / "odd.pcm"), channels=2)


def test_empty_file_rejected(tmp_path):
    (tmp_path / "empty.pcm").write_bytes(b"")
    with pytest.raises(ValueError):
        read_pcm(str(tmp_path / "empty.pcm"))


def test_unknown_format_rejected(tmp_path):
    np.zeros(4, dtype="<i2").tofile(tmp_path / "x.pcm")
    with pytest.raises(ValueError):
        read_pcm(str(tmp_path / "x.pcm"), fmt="u8")
