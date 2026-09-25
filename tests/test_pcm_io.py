"""PCM/WAV 读写与滤波器设计测试。"""

import numpy as np
import pytest

from rational_resampler.filter_design import design_anti_alias_fir
from rational_resampler.pcm_io import read_pcm, read_wav, write_pcm, write_wav
from rational_resampler.signals import sine


def test_s16le_roundtrip(tmp_path):
    x = sine(1000, 48000, 0.01, amplitude=0.5)
    p = tmp_path / "a.pcm"
    write_pcm(str(p), x, "s16le")
    y = read_pcm(str(p), "s16le")
    assert y.size == x.size
    np.testing.assert_allclose(y, x, atol=1.0 / 32768.0 + 1e-12)


def test_f32le_roundtrip(tmp_path):
    x = sine(440, 48000, 0.01, amplitude=0.25)
    p = tmp_path / "a.f32"
    write_pcm(str(p), x, "f32le")
    y = read_pcm(str(p), "f32le")
    np.testing.assert_allclose(y, x, atol=1e-7)


def test_wav_roundtrip(tmp_path):
    x = sine(440, 48000, 0.01, amplitude=0.25)
    p = tmp_path / "a.wav"
    write_wav(str(p), x, 48000)
    y, fs = read_wav(str(p))
    assert fs == 48000
    np.testing.assert_allclose(y, x, atol=1.0 / 32768.0 + 1e-12)


def test_write_clips_out_of_range(tmp_path):
    x = np.array([2.0, -2.0, 0.0])
    p = tmp_path / "clip.pcm"
    write_pcm(str(p), x, "s16le")
    y = read_pcm(str(p), "s16le")
    assert y[0] <= 1.0 and y[1] >= -1.0


def test_bad_format_rejected(tmp_path):
    with pytest.raises(ValueError):
        read_pcm(str(tmp_path / "x"), "u8")


def test_filter_dc_gain_and_parity():
    """直流增益 = L（补偿零插值），长度为奇数（群延迟为整数样本）。"""
    for up, down in [(3, 2), (2, 3), (1, 2), (2, 1)]:
        h = design_anti_alias_fir(up, down)
        assert h.size % 2 == 1
        assert abs(h.sum() - up) < 1e-10
        np.testing.assert_allclose(h, h[::-1], atol=0.0)  # 严格对称


def test_filter_stopband_attenuation():
    """频域抽检：阻带（新奈奎斯特以上）衰减 >= 设计值余量内。"""
    up, down = 1, 2
    att = 80.0
    h = design_anti_alias_fir(up, down, attenuation_db=att)
    nfft = 1 << 16
    H = np.abs(np.fft.rfft(h, nfft))
    freqs = np.fft.rfftfreq(nfft)  # cycles/sample，对上采样率(=fs_in)归一化
    # 阻带起点：f_nyq = 0.5/down = 0.25，留过渡带余量后从 0.26 起检
    mask = freqs >= 0.26
    worst_db = 20.0 * np.log10(H[mask].max() / up)
    assert worst_db <= -(att - 5.0), f"stopband only {-worst_db:.1f} dB"
