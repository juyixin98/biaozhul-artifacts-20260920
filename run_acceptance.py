#!/usr/bin/env python3
"""验收脚本：脉冲、通带正弦、超奈奎斯特混叠、分块一致性。

运行：
    python3 run_acceptance.py
退出码 0 表示全部通过；每项打印实测数值，便于如实记录。
"""

import sys

import numpy as np

from resample_service import RationalResampler
from resample_service import synth


def fit_sine(y, freq, fs):
    n = len(y)
    t = np.arange(n) / fs
    A = np.column_stack([np.cos(2 * np.pi * freq * t),
                         np.sin(2 * np.pi * freq * t)])
    coef, *_ = np.linalg.lstsq(A, y, rcond=None)
    resid = y - A @ coef
    return float(np.hypot(*coef)), float(np.sqrt(np.mean(resid**2)))


def check(name, ok, detail):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    return bool(ok)


def main():
    results = []

    # ------------------------------------------------------------------
    print("== 1. 脉冲：长度、位置、群延迟 ==")
    up, down, n_in, k0 = 3, 2, 2000, 500
    rs = RationalResampler(up, down)
    x = synth.impulse(n_in, index=k0)
    y = rs.process(x)
    exp_len = -(-n_in * up // down)
    results.append(check(
        "输出长度", len(y) == exp_len,
        f"N_in={n_in}, L/M={up}/{down} -> N_out={len(y)} (期望 {exp_len})"))
    exp_idx = k0 * up // down
    peak = int(np.argmax(np.abs(y)))
    results.append(check(
        "脉冲位置（群延迟已补偿）", abs(peak - exp_idx) <= 1,
        f"峰值位于输出 #{peak} (期望 {exp_idx}=k0*L/M)"))
    results.append(check(
        "群延迟定义", rs.group_delay_up == (rs.num_taps - 1) // 2,
        f"gd = {rs.group_delay_up} 上采样样本 = {rs.group_delay_in:.1f} 输入样本"
        f" = {rs.group_delay_seconds(48000)*1e3:.3f} ms @48kHz"))
    peak_err = abs(y[peak] - up * rs.h[rs.group_delay_up])
    results.append(check(
        "脉冲峰值 = L*h[gd]", peak_err < 1e-12,
        f"|y[peak] - L*h[gd]| = {peak_err:.2e}"))

    # ------------------------------------------------------------------
    print("== 2. 正弦：通带误差 (48 kHz -> 32 kHz, L/M=2/3) ==")
    fs_in, fs_out, freq = 48000.0, 32000.0, 3000.0
    rs = RationalResampler(2, 3)
    x = synth.sine(fs_in, freq, duration=0.25)
    y = rs.process(x)
    margin = int(np.ceil(rs.group_delay_in * fs_out / fs_in)) + 20
    amp, resid = fit_sine(y[margin:-margin], freq, fs_out)
    results.append(check(
        "通带幅度误差", abs(amp - 1.0) < 1e-3,
        f"拟合幅度 {amp:.8f} (误差 {amp - 1.0:+.2e})"))
    results.append(check(
        "通带残差", resid < 1e-4,
        f"拟合残差 RMS {resid:.2e}"))

    # ------------------------------------------------------------------
    print("== 3. 高于新奈奎斯特的信号：混叠抑制 ==")
    rs = RationalResampler(2, 3, atten_db=80.0)
    x = synth.multitone(fs_in, [(20000.0, 1.0), (4000.0, 1.0)], duration=0.25)
    y = rs.process(x)
    seg = y[margin:-margin]
    amp_alias, _ = fit_sine(seg, 12000.0, fs_out)  # 20kHz 混叠到 12kHz
    amp_ref, _ = fit_sine(seg, 4000.0, fs_out)
    supp = 20 * np.log10(amp_alias / amp_ref)
    results.append(check(
        "混叠抑制", supp < -70.0,
        f"20 kHz 分量（新奈奎斯特 16 kHz 以上）混叠到 12 kHz，"
        f"相对通带参考 {supp:.1f} dB"))

    # ------------------------------------------------------------------
    print("== 4. 分块处理与整段处理一致性 ==")
    for pad_mode in ("zero", "reflect"):
        rs = RationalResampler(2, 3, pad_mode=pad_mode)
        x = synth.noise(10000, seed=7)
        y_whole = rs.process(x)
        for bs in (1, 13, 256, 1024, 4096):
            y_block = rs.process_blocks(x, bs)
            diff = float(np.max(np.abs(y_block - y_whole)))
            results.append(check(
                f"pad={pad_mode}, block={bs}", diff < 1e-12,
                f"最大偏差 {diff:.3e}, 长度 {len(y_block)}/{len(y_whole)}"))

    # ------------------------------------------------------------------
    n_pass = sum(results)
    print(f"\n总计 {n_pass}/{len(results)} 项通过")
    return 0 if n_pass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
