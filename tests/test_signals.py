"""Tests for synthetic signals and PCM I/O."""

import numpy as np
import pytest

from fixed_iir import signals


def test_sine_properties():
    x = signals.sine(1000, 0.01, amplitude=0.5)
    assert x.shape == (1000,)
    assert np.max(np.abs(x)) <= 0.5 + 1e-12
    # RMS of a sinusoid is amplitude/sqrt(2)
    assert np.sqrt(np.mean(x**2)) == pytest.approx(0.5 / np.sqrt(2), rel=1e-2)


def test_multi_tone_and_chirp():
    x = signals.multi_tone(512, [(0.05, 0.3), (0.2, 0.2)])
    assert np.max(np.abs(x)) <= 0.5 + 1e-12
    c = signals.chirp(512, 0.0, 0.5, 0.8)
    assert np.max(np.abs(c)) <= 0.8 + 1e-12


def test_impulse_and_noise():
    x = signals.impulse(64, amplitude=0.7, at=5)
    assert x[5] == 0.7 and np.count_nonzero(x) == 1
    n1 = signals.noise(128, seed=42)
    n2 = signals.noise(128, seed=42)
    np.testing.assert_array_equal(n1, n2)  # seeded => deterministic


def test_pcm_roundtrip(tmp_path):
    x = np.linspace(-0.9, 0.9, 1000)
    p = tmp_path / "sig.pcm"
    signals.write_pcm(str(p), x, "int16")
    y = signals.read_pcm(str(p), "int16")
    assert np.max(np.abs(x - y)) < 2 / 32768.0


def test_pcm_write_saturates(tmp_path):
    x = np.array([2.0, -2.0, 0.5])
    p = tmp_path / "sat.pcm"
    signals.write_pcm(str(p), x, "int16")
    y = signals.read_pcm(str(p), "int16")
    assert y[0] == pytest.approx(32767 / 32768.0)
    assert y[1] == pytest.approx(-1.0)


def test_pcm_dtypes(tmp_path):
    x = np.linspace(-0.5, 0.5, 100)
    for dt in ("int16", "int32", "uint8"):
        p = tmp_path / f"sig_{dt}.pcm"
        signals.write_pcm(str(p), x, dt)
        y = signals.read_pcm(str(p), dt)
        tol = {"int16": 2 / 2**15, "int32": 2 / 2**31, "uint8": 2 / 128}[dt]
        assert np.max(np.abs(x - y)) < tol
    with pytest.raises(ValueError):
        signals.read_pcm(str(tmp_path / "x.pcm"), "float64")


def test_csv_roundtrip(tmp_path):
    x = np.array([0.1, -0.2, 0.3])
    p = tmp_path / "sig.csv"
    signals.write_csv(str(p), x)
    y = np.loadtxt(str(p))
    np.testing.assert_allclose(y, x, atol=1e-9)
