"""核心正确性测试：朴素参考实现对照、长度约定、分块一致性。"""

import math

import numpy as np
import pytest

from rational_resampler import (
    PolyphaseResampler,
    StreamingResampler,
    design_anti_alias_fir,
    resample,
)
from rational_resampler.signals import noise, sine


def naive_resample(x, h, up, down):
    """独立朴素参考：直接按定义式双重循环，输入界外补零。"""
    x = np.asarray(x, dtype=np.float64)
    n_taps = h.size
    delay = (n_taps - 1) // 2  # 群延迟补偿项 D
    n_out = -(-x.size * up // down) if x.size else 0
    y = np.zeros(n_out)
    for m in range(n_out):
        acc = 0.0
        for i in range(x.size):
            j = m * down + delay - i * up
            if 0 <= j < n_taps:
                acc += x[i] * h[j]
        y[m] = acc
    return y


@pytest.mark.parametrize("up,down", [(3, 2), (2, 3), (1, 2), (2, 1), (5, 7), (1, 1)])
def test_output_length_convention(up, down):
    """长度约定：n_out = ceil(n_in * L / M)。"""
    h = design_anti_alias_fir(up, down)
    for n_in in (1, 2, 100, 1000):
        x = noise(n_in, seed=42)
        y = resample(x, up, down, h=h)
        assert y.size == math.ceil(n_in * up / down)


@pytest.mark.parametrize("up,down", [(3, 2), (2, 3), (1, 2)])
def test_matches_naive_reference(up, down):
    """pad_mode='none' 时必须与朴素定义式逐样本一致。"""
    h = design_anti_alias_fir(up, down)
    x = noise(200, seed=1)
    y = resample(x, up, down, h=h, pad_mode="none")
    y_ref = naive_resample(x, h, up, down)
    np.testing.assert_allclose(y, y_ref, rtol=1e-12, atol=1e-12)


@pytest.mark.parametrize("pad_mode", ["zero", "edge", "reflect"])
@pytest.mark.parametrize("up,down", [(3, 2), (2, 3)])
def test_block_equals_whole(pad_mode, up, down):
    """分块处理与整段处理逐样本一致（同一状态机，按构造 bitwise 相等）。"""
    h = design_anti_alias_fir(up, down)
    x = noise(997, seed=7)
    whole = resample(x, up, down, h=h, pad_mode=pad_mode)
    for block in (1, 2, 3, 7, 100, 500, 997):
        r = StreamingResampler(up, down, h=h, pad_mode=pad_mode)
        parts = [r.process(x[i : i + block]) for i in range(0, x.size, block)]
        parts.append(r.finish())
        y = np.concatenate([p for p in parts if p.size])
        assert y.size == whole.size, f"block={block}"
        np.testing.assert_array_equal(y, whole, err_msg=f"block={block}")


def test_core_flush_drains_filter_tail():
    """flush 后核心总输出数覆盖完整滤波器尾部。"""
    up, down = 3, 2
    h = design_anti_alias_fir(up, down)
    n_in = 50
    core = PolyphaseResampler(h, up, down)
    y1 = core.process(noise(n_in, seed=3))
    y2 = core.flush()
    total = y1.size + y2.size
    delay = (h.size - 1) // 2
    expected = ((n_in - 1) * up - delay + h.size - 1) // down + 1
    assert total == expected


def test_zero_phase_alignment():
    """零相位对齐：通带正弦重采样后与理想序列在稳态区误差极小（无群延迟偏移）。"""
    up, down = 3, 2
    fs_in = 48000.0
    fs_out = fs_in * up / down
    freq = 1000.0
    x = sine(freq, fs_in, 0.1)
    y = resample(x, up, down)
    t = np.arange(y.size) / fs_out
    ideal = np.sin(2.0 * np.pi * freq * t)
    skip = 200  # 跳过边界暂态
    err = y[skip:-skip] - ideal[skip:-skip]
    assert np.max(np.abs(err)) < 1e-3


def test_empty_input():
    y = resample(np.zeros(0), 3, 2)
    assert y.size == 0


def test_short_input_reflect_padding():
    """输入短于填充长度时 reflect 也不得崩溃。"""
    x = np.array([1.0, -1.0, 0.5])
    y = resample(x, 3, 2, pad_mode="reflect")
    assert y.size == math.ceil(3 * 3 / 2)


def test_invalid_pad_mode_rejected():
    with pytest.raises(ValueError):
        StreamingResampler(3, 2, pad_mode="bogus")
