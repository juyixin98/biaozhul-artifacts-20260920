"""验收测试：脉冲、通带正弦、高于新奈奎斯特频率信号。

验收项：
1. 脉冲 —— 输出长度符合约定，脉冲响应与定义式一致，峰位置正确；
2. 正弦（通带）—— 稳态通带误差 < 1e-3，幅度保持；
3. 高于新奈奎斯特的正弦 —— 混叠抑制 >= 60 dB。
"""

import math

import numpy as np

from rational_resampler import design_anti_alias_fir, resample
from rational_resampler.metrics import sine_fit, suppression_db
from rational_resampler.service import run_job
from rational_resampler.signals import impulse, sine


def test_acceptance_impulse():
    """脉冲：长度、波形（对照定义式）、峰值位置。"""
    up, down = 3, 2
    n_in = 512
    i0 = 256
    h = design_anti_alias_fir(up, down)
    x = impulse(n_in, index=i0)
    y = resample(x, up, down, h=h, pad_mode="none")

    # 1) 长度约定
    assert y.size == math.ceil(n_in * up / down) == 768

    # 2) 波形即延迟补偿后的滤波器脉冲响应采样：y[m] = h[m*M + D - i0*L]
    delay = (h.size - 1) // 2
    expected = np.zeros(y.size)
    for m in range(y.size):
        j = m * down + delay - i0 * up
        if 0 <= j < h.size:
            expected[m] = h[j]
    np.testing.assert_allclose(y, expected, atol=1e-15)

    # 3) 峰值位置：i0 * L/M 附近（±1 样本）
    peak = int(np.argmax(np.abs(y)))
    assert abs(peak - i0 * up / down) <= 1


def test_acceptance_passband_sine():
    """通带正弦：1 kHz @ 48 kHz，3/2 重采样到 72 kHz，稳态误差与幅度。"""
    up, down = 3, 2
    fs_in, freq = 48000.0, 1000.0
    fs_out = fs_in * up / down
    x = sine(freq, fs_in, 0.2)
    y = resample(x, up, down)
    assert y.size == math.ceil(x.size * up / down)

    fit = sine_fit(y, freq, fs_out, skip=400)
    assert fit["residual_rms"] < 1e-3, fit
    assert fit["residual_max"] < 5e-3, fit
    assert abs(fit["amplitude"] - 1.0) < 0.01  # 通带幅度保持（纹波 << 1%）


def test_acceptance_alias_suppression():
    """高于新奈奎斯特的正弦：20 kHz @ 48 kHz，1/2 抽取到 24 kHz。

    新奈奎斯特 = 12 kHz，20 kHz 分量必须被抗混叠 FIR 抑制 >= 60 dB。
    """
    up, down = 1, 2
    fs_in, freq = 48000.0, 20000.0
    fs_out = fs_in * up / down
    assert freq > fs_out / 2.0
    x = sine(freq, fs_in, 0.1)
    y = resample(x, up, down)
    assert y.size == math.ceil(x.size * up / down)

    sup = suppression_db(y, reference_amplitude=1.0, skip=400)
    assert sup >= 60.0, f"alias suppression only {sup:.1f} dB"


def test_acceptance_via_service_job(tmp_path):
    """通过服务请求跑验收：报告数值齐全且输出文件写盘。"""
    out = tmp_path / "sine_72k.wav"
    rep = tmp_path / "report.json"
    report = run_job({
        "signal": {"type": "sine", "freq": 1000, "fs": 48000, "duration": 0.1},
        "up": 3, "down": 2,
        "output": str(out), "output_format": "wav",
        "report": str(rep),
        "block_size": 333,  # 服务内部分块路径
    })
    assert report["n_out"] == report["expected_n_out"] == 7200
    assert report["fs_out"] == 72000.0
    assert report["sine_fit"]["residual_rms"] < 1e-3
    assert out.exists() and rep.exists()

    # 整段与分块走服务结果一致
    report_whole = run_job({
        "signal": {"type": "sine", "freq": 1000, "fs": 48000, "duration": 0.1},
        "up": 3, "down": 2,
    })
    assert report_whole["n_out"] == report["n_out"]
