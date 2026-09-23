"""自动化测试：长度、群延迟、通带误差、混叠抑制、分块一致性。"""

import numpy as np
import pytest

from resample_service import RationalResampler
from resample_service.filters import design_lowpass, frequency_response
from resample_service import synth


# ----------------------------------------------------------------------
# 辅助
# ----------------------------------------------------------------------
def fit_sine(y: np.ndarray, freq: float, fs: float):
    """最小二乘拟合 y = a*cos + b*sin，返回 (幅度, 相位, 残差RMS)。"""
    n = len(y)
    t = np.arange(n) / fs
    c = np.cos(2 * np.pi * freq * t)
    s = np.sin(2 * np.pi * freq * t)
    A = np.column_stack([c, s])
    coef, *_ = np.linalg.lstsq(A, y, rcond=None)
    resid = y - A @ coef
    amp = float(np.hypot(*coef))
    phase = float(np.arctan2(coef[1], coef[0]))
    return amp, phase, float(np.sqrt(np.mean(resid**2)))


# ----------------------------------------------------------------------
# 滤波器设计
# ----------------------------------------------------------------------
def test_filter_specs():
    h = design_lowpass(num_taps=255, cutoff=0.15, atten_db=80.0)
    assert abs(h.sum() - 1.0) < 1e-12
    f = np.linspace(0, 0.5, 4001)
    mag = np.abs(frequency_response(h, f))
    db = 20 * np.log10(mag + 1e-300)
    pb = db[f <= 0.135]           # 通带（过渡带起点以内）
    sb = db[f >= 0.165]           # 阻带（过渡带终点以外）
    assert pb.max() < 0.1 and pb.min() > -0.1, "通带波纹应 < 0.1 dB"
    assert sb.max() < -70.0, "阻带衰减应 > 70 dB"


# ----------------------------------------------------------------------
# 输出长度
# ----------------------------------------------------------------------
@pytest.mark.parametrize("n_in,up,down", [
    (1, 1, 1), (100, 3, 2), (1000, 2, 3), (999, 2, 3),
    (4800, 147, 160), (12345, 1, 4), (777, 5, 1), (2048, 7, 5),
])
def test_output_length(n_in, up, down):
    rs = RationalResampler(up, down)
    x = np.zeros(n_in)
    y = rs.process(x)
    expected = -(-n_in * up // down)  # ceil
    assert len(y) == expected == rs.output_length(n_in)


# ----------------------------------------------------------------------
# 脉冲：群延迟与位置
# ----------------------------------------------------------------------
def test_impulse_position_and_group_delay():
    up, down = 3, 2
    rs = RationalResampler(up, down)
    n_in, k0 = 2000, 500
    x = synth.impulse(n_in, index=k0)
    y = rs.process(x)
    # 输入第 k0 个样本应对齐输出第 k0*L/M 个样本（群延迟已补偿）
    expected_idx = k0 * up // down
    assert expected_idx * down == k0 * up  # 本例恰好整除
    peak = int(np.argmax(np.abs(y)))
    assert abs(peak - expected_idx) <= 1
    # 峰值应等于 L * h[gd]（零插值增益补偿）
    assert abs(y[peak] - up * rs.h[rs.group_delay_up]) < 1e-12
    # 群延迟定义自洽：gd(输入样本) = gd_up / L
    assert rs.group_delay_in == rs.group_delay_up / up


# ----------------------------------------------------------------------
# 正弦：通带误差
# ----------------------------------------------------------------------
def test_sine_passband_error():
    fs_in, fs_out = 48000.0, 32000.0  # L/M = 2/3
    freq = 3000.0                     # 远在通带内（截止约 0.15*fs_up）
    rs = RationalResampler(2, 3)
    x = synth.sine(fs_in, freq, duration=0.25)
    y = rs.process(x)
    # 去掉两端受边界填充影响的区域
    margin = int(np.ceil(rs.group_delay_in * fs_out / fs_in)) + 20
    amp, _, resid = fit_sine(y[margin:-margin], freq, fs_out)
    assert abs(amp - 1.0) < 1e-3, f"幅度误差 {amp - 1.0:+.2e}"
    assert resid < 1e-4, f"拟合残差 RMS {resid:.2e}"


# ----------------------------------------------------------------------
# 高于新奈奎斯特频率的信号：混叠抑制
# ----------------------------------------------------------------------
def test_alias_suppression():
    fs_in = 48000.0
    fs_out = 32000.0                  # 新奈奎斯特 16 kHz
    f_hi = 20000.0                    # 高于新奈奎斯特，应被滤除
    f_ref = 4000.0                    # 通带参考
    rs = RationalResampler(2, 3, atten_db=80.0)
    x = synth.multitone(fs_in, [(f_hi, 1.0), (f_ref, 1.0)], duration=0.25)
    y = rs.process(x)
    margin = int(np.ceil(rs.group_delay_in * fs_out / fs_in)) + 20
    seg = y[margin:-margin]
    # 20000 Hz 混叠到 |20000 - 32000| = 12000 Hz
    amp_alias, _, _ = fit_sine(seg, 12000.0, fs_out)
    amp_ref, _, _ = fit_sine(seg, f_ref, fs_out)
    suppression_db = 20 * np.log10(amp_alias / amp_ref)
    assert suppression_db < -70.0, f"混叠抑制仅 {suppression_db:.1f} dB"


# ----------------------------------------------------------------------
# 分块与整段一致性
# ----------------------------------------------------------------------
@pytest.mark.parametrize("up,down", [(3, 2), (2, 3), (1, 4), (5, 3)])
@pytest.mark.parametrize("block_size", [1, 13, 256, 1024, 4096])
@pytest.mark.parametrize("pad_mode", ["zero", "reflect"])
def test_block_equals_whole(up, down, block_size, pad_mode):
    rs = RationalResampler(up, down, pad_mode=pad_mode)
    x = synth.noise(5000, seed=42)
    y_whole = rs.process(x)
    y_block = rs.process_blocks(x, block_size)
    assert len(y_block) == len(y_whole)
    diff = float(np.max(np.abs(y_block - y_whole))) if len(y_whole) else 0.0
    assert diff < 1e-12, f"分块/整段最大偏差 {diff:.3e}"


# ----------------------------------------------------------------------
# 服务层（JSON 请求 -> 文件）
# ----------------------------------------------------------------------
def test_service_roundtrip(tmp_path):
    import json
    from resample_service.service import run_request

    req = {
        "input": {"type": "sine", "sample_rate": 48000,
                  "frequency": 1000.0, "duration": 0.05},
        "resample": {"target_sample_rate": 32000},
        "output": {"path": "out.f64le", "format": "f64le",
                   "report_path": "out.report.json"},
    }
    (tmp_path / "req.json").write_text(json.dumps(req))
    report = run_request(req, base_dir=tmp_path)
    assert report["ok"]
    y = np.fromfile(tmp_path / "out.f64le", dtype="<f8")
    assert len(y) == report["output"]["length"] == 1600
    assert (tmp_path / "out.report.json").exists()
