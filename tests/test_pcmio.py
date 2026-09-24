"""PCM / WAV I/O 往返测试。"""

import numpy as np
import pytest

from delay_correlator.pcmio import (
    deinterleave,
    read_pcm,
    read_wav,
    write_pcm,
    write_wav,
)


@pytest.mark.parametrize("dtype,rtol,atol", [
    ("s16", 1e-3, 2e-4),
    ("s32", 1e-6, 1e-7),
    ("f32", 1e-6, 1e-7),
    ("u8", 2e-2, 2e-2),
])
def test_pcm_roundtrip(tmp_path, dtype, rtol, atol):
    rng = np.random.default_rng(0)
    data = rng.uniform(-0.9, 0.9, size=(1000, 2))
    path = tmp_path / f"stereo.{dtype}.pcm"
    write_pcm(str(path), data, dtype=dtype)
    back = read_pcm(str(path), dtype=dtype, channels=2)
    assert back.shape == (1000, 2)
    np.testing.assert_allclose(back, data, rtol=rtol, atol=atol)


def test_interleaved_order_preserved(tmp_path):
    data = np.column_stack([np.linspace(-0.5, 0.5, 10),
                           np.linspace(0.5, -0.5, 10)])
    path = tmp_path / "x.s16.pcm"
    write_pcm(str(path), data, dtype="s16")
    a, b = deinterleave(read_pcm(str(path), dtype="s16", channels=2))
    np.testing.assert_allclose(a, data[:, 0], atol=2e-4)
    np.testing.assert_allclose(b, data[:, 1], atol=2e-4)


def test_bad_channel_division_raises(tmp_path):
    path = tmp_path / "odd.s16.pcm"
    (np.arange(3).astype(np.int16)).tofile(str(path))
    with pytest.raises(ValueError):
        read_pcm(str(path), dtype="s16", channels=2)


@pytest.mark.parametrize("bits", [8, 16, 32])
def test_wav_roundtrip(tmp_path, bits):
    rng = np.random.default_rng(1)
    data = rng.uniform(-0.8, 0.8, size=(4000, 2))
    path = tmp_path / f"stereo-{bits}.wav"
    write_wav(str(path), data, 16000, bits=bits)
    back, sr = read_wav(str(path))
    assert sr == 16000
    assert back.shape == (4000, 2)
    tol = {8: 2e-2, 16: 2e-4, 32: 1e-7}[bits]
    np.testing.assert_allclose(back, data, atol=tol)


def test_wav_24bit_roundtrip(tmp_path):
    rng = np.random.default_rng(2)
    data = rng.uniform(-0.5, 0.5, size=(2000, 1))
    import wave

    # 手工生成 24-bit WAV
    clipped = np.clip(np.rint(data.ravel() * 8388608.0), -8388608, 8388607).astype("<i4")
    b3 = ((clipped[:, None] & np.array([0xFF, 0xFF00, 0xFF0000])) >>
          np.array([0, 8, 16])).astype("<u1")
    with wave.open(str(tmp_path / "x24.wav"), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(3)
        wf.setframerate(48000)
        wf.writeframes(b3.tobytes())
    back, sr = read_wav(str(tmp_path / "x24.wav"))
    assert sr == 48000
    np.testing.assert_allclose(back.ravel(), data.ravel(), atol=2e-7)


def test_unsupported_dtype():
    with pytest.raises(ValueError):
        read_pcm("nonexistent", dtype="f64")
