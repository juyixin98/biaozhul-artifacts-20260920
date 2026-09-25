"""合成信号与 PCM 读写测试。"""

import numpy as np
import pytest

from burst_detector.signal_io import (
    add_drift,
    add_spikes,
    add_step,
    gaussian_noise,
    inject_missing,
    make_scenario,
    read_pcm,
    write_pcm,
)


def test_reproducible_noise():
    np.testing.assert_array_equal(
        gaussian_noise(100, seed=5), gaussian_noise(100, seed=5)
    )
    assert not np.array_equal(
        gaussian_noise(100, seed=5), gaussian_noise(100, seed=6)
    )


def test_step_is_persistent_from_onset():
    x = np.zeros(100)
    y = add_step(x, at=40, amplitude=3.0)
    np.testing.assert_array_equal(y[:40], 0.0)
    np.testing.assert_array_equal(y[40:], 3.0)
    # 不修改原数组。
    np.testing.assert_array_equal(x, 0.0)


def test_drift_is_linear_and_zero_before_onset():
    x = np.zeros(100)
    y = add_drift(x, at=50, rate=0.5)
    np.testing.assert_array_equal(y[:50], 0.0)
    np.testing.assert_allclose(y[50:], 0.5 * np.arange(50))


def test_spikes_are_isolated():
    y = add_spikes(np.zeros(100), [10, 90], amplitudes=[5.0, -7.0])
    assert y[10] == 5.0
    assert y[90] == -7.0
    assert y[[9, 11, 89, 91]].sum() == 0.0


def test_inject_missing():
    y = inject_missing(np.ones(10), [3])
    assert np.isnan(y[3])
    assert np.isfinite(y).sum() == 9


def test_make_scenario_kinds():
    for kind in ("clean", "step", "drift", "spike"):
        x = make_scenario(kind, n=500)
        assert x.shape == (500,)
        assert np.isfinite(x).all()
    with pytest.raises(ValueError):
        make_scenario("nonsense")


@pytest.mark.parametrize("dtype", ["int16", "int32", "uint8", "float32"])
def test_pcm_roundtrip(tmp_path, dtype):
    rng = np.random.default_rng(0)
    # 幅度限制在 ±0.8 以内，避免整型满量程处的合法削顶污染往返比较。
    x = np.tanh(0.5 * rng.standard_normal(1000)) * 0.8
    path = tmp_path / f"sig_{dtype}.pcm"
    n = write_pcm(str(path), x, dtype=dtype)
    assert n == 1000
    y = read_pcm(str(path), dtype=dtype)
    # 整型量化有误差，容差随格式位宽变化。
    tol = {"int16": 2e-4, "int32": 1e-9, "uint8": 1e-2, "float32": 1e-6}[dtype]
    np.testing.assert_allclose(y, x, atol=tol)


def test_pcm_multichannel(tmp_path):
    x = np.arange(12, dtype=np.int16).reshape(-1, 2)
    path = tmp_path / "stereo.pcm"
    x.tofile(path)
    ch0 = read_pcm(str(path), dtype="int16", channels=2, channel=0, normalize=False)
    ch1 = read_pcm(str(path), dtype="int16", channels=2, channel=1, normalize=False)
    avg = read_pcm(str(path), dtype="int16", channels=2, channel="avg",
                   normalize=False)
    np.testing.assert_array_equal(ch0, np.arange(0, 12, 2))
    np.testing.assert_array_equal(ch1, np.arange(1, 12, 2))
    np.testing.assert_array_equal(avg, (ch0 + ch1) / 2)


def test_pcm_bad_channel_count(tmp_path):
    path = tmp_path / "x.pcm"
    np.arange(10, dtype=np.int16).tofile(path)
    with pytest.raises(ValueError):
        read_pcm(str(path), channels=3)


def test_write_pcm_rejects_nonfinite(tmp_path):
    with pytest.raises(ValueError):
        write_pcm(str(tmp_path / "x.pcm"), np.array([1.0, np.nan]))


def test_unknown_dtype_rejected():
    with pytest.raises(ValueError):
        read_pcm("whatever", dtype="int8")
