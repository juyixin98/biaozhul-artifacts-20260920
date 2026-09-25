"""验收测试：带偏置正弦、偏置跳变、极短块、分块不变性、无未来泄漏。"""

import json
import math
import os

import numpy as np
import pytest

from dcblocker import DCBlocker
from dcblocker.filter import compute_coefficient
from dcblocker.service import run_request

FS = 48000.0
FC = 5.0


def reference_process(x, r):
    """逐样本参考实现（慢但显然正确），用于交叉验证向量化结果。"""
    y = np.empty(len(x), dtype=np.float64)
    x_prev, y_prev = 0.0, 0.0
    for n, xn in enumerate(x):
        y_prev = xn - x_prev + r * y_prev
        x_prev = xn
        y[n] = y_prev
    return y


def run_in_blocks(blocker, x, block_size):
    parts = [blocker.process(x[i : i + block_size]) for i in range(0, len(x), block_size)]
    return np.concatenate(parts) if parts else np.empty(0)


# ---------- 带偏置正弦：稳态误差 ----------

def test_biased_sine_steady_state_error():
    t = np.arange(int(FS * 2.0)) / FS
    x = 0.5 * np.sin(2 * np.pi * 440 * t) + 0.3
    y = run_in_blocks(DCBlocker(FS, FC), x, 1024)

    tail = y[int(FS * 1.0):]  # 第 2 秒为稳态
    assert abs(np.mean(tail)) < 1e-3          # 残余 DC ≈ 0
    # 440 Hz 远高于 5 Hz 截止，幅度应基本无衰减
    assert np.max(np.abs(tail)) == pytest.approx(0.5, rel=0.01)


def test_constant_dc_removed():
    x = np.full(int(FS), 0.7)
    y = run_in_blocks(DCBlocker(FS, FC), x, 480)
    assert abs(np.mean(y[int(FS * 0.5):])) < 1e-3


# ---------- 偏置跳变：响应时间 ----------

def test_bias_step_response_time_matches_theory():
    blocker = DCBlocker(FS, FC)
    r = blocker.coefficient
    amplitude = 0.6
    x = np.full(int(FS * 1.0), amplitude)
    y = run_in_blocks(blocker, x, 1024)

    # 理论：阶跃响应 y[n] = A * R**n，1% 稳定点 n* = ln(0.01)/ln(R)
    n_star = math.log(0.01) / math.log(r)
    settled = np.nonzero(np.abs(y) > 0.01 * amplitude)[0]
    n_measured = settled[-1] + 1  # 最后一个超出 1% 的样本之后即稳定
    assert n_measured == pytest.approx(n_star, rel=0.02)


def test_bias_jump_midstream():
    """流中途偏置从 +0.2 跳到 -0.4，跳变后应按同一时间常数重新收敛。"""
    blocker = DCBlocker(FS, FC)
    r = blocker.coefficient
    n1 = int(FS * 1.0)
    x = np.concatenate([np.full(n1, 0.2), np.full(n1, -0.4)])
    y = run_in_blocks(blocker, x, 512)

    seg = y[n1:]  # 跳变后段落，理论形状 ≈ -0.6 * R**k
    k = np.arange(n1)
    expected = -0.6 * r ** k
    # 跳变瞬间输出应接近 -0.6（偏置差），随后指数衰减
    assert seg[0] == pytest.approx(-0.6, abs=1e-3)
    np.testing.assert_allclose(seg, expected, atol=1e-3)
    # 跳变后 0.5 s 应已充分稳定
    assert np.max(np.abs(seg[int(FS * 0.5):])) < 1e-3


# ---------- 极短块 ----------

@pytest.mark.parametrize("block_size", [1, 2, 3, 5])
def test_extremely_short_blocks(block_size):
    rng = np.random.default_rng(42)
    x = rng.standard_normal(1000) + 0.4
    y = run_in_blocks(DCBlocker(FS, FC), x, block_size)
    np.testing.assert_array_equal(y, run_in_blocks(DCBlocker(FS, FC), x, 1000))


def test_empty_block():
    blocker = DCBlocker(FS, FC)
    assert blocker.process([]).size == 0
    y = blocker.process([0.5, 0.6])
    assert y.shape == (2,)


# ---------- 分块不变性（逐位一致） ----------

@pytest.mark.parametrize("block_size", [1, 7, 100, 1024, 4096, 4097, 96000])
def test_block_size_invariance(block_size):
    rng = np.random.default_rng(7)
    x = rng.standard_normal(20000) * 0.3 + 0.25
    y = run_in_blocks(DCBlocker(FS, FC), x, block_size)
    whole = DCBlocker(FS, FC).process(x)
    np.testing.assert_array_equal(y, whole)  # 严格相等，不是近似


# ---------- 无未来泄漏（不用整段均值） ----------

def test_causality_no_future_leakage():
    """两条共享前缀的信号，前缀部分输出必须完全一致。"""
    rng = np.random.default_rng(1)
    prefix = rng.standard_normal(5000) + 0.5
    x1 = np.concatenate([prefix, np.full(5000, 9.9)])
    x2 = np.concatenate([prefix, np.full(5000, -9.9)])
    y1 = DCBlocker(FS, FC).process(x1)
    y2 = DCBlocker(FS, FC).process(x2)
    np.testing.assert_array_equal(y1[:5000], y2[:5000])


def test_not_global_mean_subtraction():
    """若是整段均值法，首样本输出会是 x[0]-mean；一阶高通应为 x[0] 本身。"""
    x = np.full(1000, 0.8)
    y = DCBlocker(FS, FC).process(x)
    assert y[0] == pytest.approx(0.8)


# ---------- 向量化与参考实现一致 ----------

def test_vectorized_matches_reference_loop():
    rng = np.random.default_rng(3)
    x = rng.standard_normal(30000) + 0.1
    y = DCBlocker(FS, FC).process(x)
    np.testing.assert_allclose(y, reference_process(x, compute_coefficient(FS, FC)),
                               rtol=1e-9, atol=1e-12)


# ---------- 状态重置与采样率重设 ----------

def test_reset_restores_initial_state():
    blocker = DCBlocker(FS, FC)
    x = np.full(500, 0.5)
    blocker.process(x)
    blocker.reset()
    np.testing.assert_array_equal(blocker.process(x), DCBlocker(FS, FC).process(x))


def test_set_sample_rate_recomputes_coefficient():
    blocker = DCBlocker(FS, FC)
    blocker.set_sample_rate(44100)
    assert blocker.sample_rate == 44100
    assert blocker.coefficient == pytest.approx(math.exp(-2 * math.pi * FC / 44100))


def test_set_sample_rate_with_reset():
    blocker = DCBlocker(FS, FC)
    blocker.process(np.full(100, 1.0))
    blocker.set_sample_rate(44100, reset_state=True)
    y = blocker.process([0.5])
    assert y[0] == pytest.approx(0.5)  # 状态已清零，首样本直通


def test_invalid_params_rejected():
    with pytest.raises(ValueError):
        DCBlocker(0)
    with pytest.raises(ValueError):
        DCBlocker(FS, cutoff_hz=-1)
    with pytest.raises(ValueError):
        DCBlocker(FS, cutoff_hz=FS / 2)
    with pytest.raises(ValueError):
        DCBlocker(FS, FC).set_sample_rate(-48000)


def test_input_not_mutated_and_2d_rejected():
    x = np.array([0.1, 0.2, 0.3])
    DCBlocker(FS, FC).process(x)
    np.testing.assert_array_equal(x, [0.1, 0.2, 0.3])
    with pytest.raises(ValueError):
        DCBlocker(FS, FC).process(np.zeros((4, 2)))


# ---------- 服务端到端 ----------

def test_service_end_to_end(tmp_path):
    out_file = tmp_path / "cleaned.f32"
    request = {
        "sample_rate": FS,
        "cutoff_hz": FC,
        "block_size": 256,
        "input": {"type": "sine", "frequency": 440, "amplitude": 0.5,
                  "dc_offset": 0.3, "duration_s": 2.0},
        "output_file": str(out_file),
    }
    resp = run_request(request)

    assert resp["samples"] == int(FS * 2.0)
    assert resp["input"]["mean"] == pytest.approx(0.3, abs=1e-3)
    assert abs(resp["steady_state_tail"]["mean"]) < 1e-3
    assert out_file.exists()

    written = np.fromfile(out_file, dtype="<f4")
    assert written.size == resp["samples"]
    assert abs(np.mean(written[int(FS):])) < 1e-3


def test_service_pcm_file_roundtrip(tmp_path):
    from dcblocker.pcm import read_pcm, write_pcm

    in_file = tmp_path / "in.i16"
    t = np.arange(int(FS * 0.5)) / FS
    write_pcm(str(in_file), 0.4 * np.sin(2 * np.pi * 220 * t) + 0.2, "int16")

    out_file = tmp_path / "out.i16"
    resp = run_request({
        "sample_rate": FS, "cutoff_hz": FC, "block_size": 128,
        "input": {"type": "pcm_file", "path": str(in_file), "format": "int16"},
        "output_file": str(out_file),
    })
    y = read_pcm(str(out_file), "int16")
    assert y.size == resp["samples"]
    assert abs(np.mean(y[int(FS * 0.25):])) < 2e-3  # 含 int16 量化误差
