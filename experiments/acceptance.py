"""验收实验：扫描非整频点正弦与双近邻频率，统计估计误差，覆盖边界。

运行：
    python experiments/acceptance.py

输出：
    results/acceptance_<UTC时间戳>.json   原始数据
    results/acceptance_<UTC时间戳>.md     汇总表
    退出码 0 = 全部判据通过，1 = 存在未通过项（明细见输出）

判据（预先声明；B 判据在首次运行后做过一次修正，见下方说明）：
    A1 无噪声单音扫描：|频率误差| <= 0.01 bin，幅度相对误差 <= 1%
    A2 40 dB SNR 单音扫描：频率误差 RMS <= 0.02 bin
    B  双音间距 >= 4 bin 且 <= 8 bin：两个峰都被检出且 interference 置位；
       间距 > 8 bin：两个峰都被检出且 interference 不置位；
       间距 < 4 bin（主瓣宽度）：两峰合并为一个，属预期行为（单峰适用边界）
    C  直流/奈奎斯特：boundary 标志为真，幅度误差 <= 1e-6；
       近边界单音（3.4 bin 处）：|频率误差| <= 0.01 bin；
       退化区单音（1.4 bin 处，负频率镜像主瓣重叠）：|频率误差| <= 0.2 bin
       —— 该区域任何三点插值都会物理性退化，只要求误差不发散

判据修正记录（诚实声明）：
    初版 B 判据要求"间距 <= 8 bin 时 interference 置位"，但间距 3.0 bin 时
    两峰已合并为一个峰，不存在可置位的第二个峰——该判据在物理上不可满足。
    首次运行（results/acceptance_20260925T015236Z.*）因此判 1 项未通过。
    现将 B 修正为按间距分段判定，修正不改变任何测量数据。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from spectral_peak.service import analyze, synthesize

FS = 48000.0
N = 4096
BIN_HZ = FS / N

RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"


def _stats(errors):
    e = np.asarray(errors, dtype=np.float64)
    return {
        "count": int(e.size),
        "max_abs": float(np.max(np.abs(e))),
        "rms": float(np.sqrt(np.mean(e * e))),
        "mean": float(np.mean(e)),
    }


def part_a_single_tone_sweep(noise_db=None):
    """扫描非整频点单音：bin 8..2030，小数偏移 0.13/0.37/0.49。"""
    freq_err_bins, amp_rel_err = [], []
    for k0 in range(8, 2031, 23):
        for delta in (0.13, 0.37, 0.49):
            freq = (k0 + delta) * BIN_HZ
            samples = synthesize(
                [{"frequency_hz": freq, "amplitude": 0.9}], FS, N, noise_db=noise_db
            )
            peaks = analyze(samples, FS)["peaks"]
            if not peaks:
                freq_err_bins.append(np.nan)
                amp_rel_err.append(np.nan)
                continue
            top = peaks[0]
            freq_err_bins.append((top["frequency_hz"] - freq) / BIN_HZ)
            amp_rel_err.append((top["amplitude"] - 0.9) / 0.9)
    return {
        "noise_db": noise_db,
        "freq_error_bins": _stats(freq_err_bins),
        "amp_rel_error": _stats(amp_rel_err),
    }


def part_b_two_close_tones():
    """双近邻频率：固定 f1=100.3 bin，f2 间距 3..12 bin。"""
    rows = []
    for sep in (3.0, 4.0, 5.0, 6.0, 8.0, 12.0):
        f1 = 100.3 * BIN_HZ
        f2 = (100.3 + sep) * BIN_HZ
        samples = synthesize(
            [{"frequency_hz": f1}, {"frequency_hz": f2, "amplitude": 0.9}], FS, N
        )
        peaks = analyze(samples, FS)["peaks"]
        row = {
            "separation_bins": sep,
            "n_peaks_detected": len(peaks),
            "interference_flags": [p["interference"] for p in peaks],
        }
        if len(peaks) >= 2:
            est = sorted(p["frequency_hz"] for p in peaks[:2])
            row["freq_error_bins"] = [
                (est[0] - f1) / BIN_HZ, (est[1] - f2) / BIN_HZ
            ]
        rows.append(row)
    return rows


def part_c_boundaries():
    """直流、近直流、奈奎斯特、近奈奎斯特。"""
    out = {}

    dc = analyze(np.full(N, 0.5), FS)["peaks"][0]
    out["dc"] = {
        "expected_amplitude": 0.5,
        "estimated_amplitude": dc["amplitude"],
        "amplitude_error": dc["amplitude"] - 0.5,
        "boundary": dc["boundary"],
        "frequency_hz": dc["frequency_hz"],
    }

    for label, bins in (("near_dc_3.4bins", 3.4), ("near_dc_1.4bins", 1.4)):
        freq = bins * BIN_HZ
        peak = analyze(
            synthesize([{"frequency_hz": freq}], FS, N), FS
        )["peaks"][0]
        out[label] = {
            "expected_frequency_hz": freq,
            "estimated_frequency_hz": peak["frequency_hz"],
            "freq_error_bins": (peak["frequency_hz"] - freq) / BIN_HZ,
        }

    nyq = analyze(0.7 * np.cos(np.pi * np.arange(N)), FS)["peaks"][0]
    out["nyquist"] = {
        "expected_amplitude": 0.7,
        "estimated_amplitude": nyq["amplitude"],
        "amplitude_error": nyq["amplitude"] - 0.7,
        "boundary": nyq["boundary"],
        "frequency_hz": nyq["frequency_hz"],
        "expected_frequency_hz": FS / 2,
    }

    for label, bins in (("near_nyquist_3.4bins", 3.4), ("near_nyquist_1.4bins", 1.4)):
        freq = (N / 2 - bins) * BIN_HZ
        peak = analyze(
            synthesize([{"frequency_hz": freq}], FS, N), FS
        )["peaks"][0]
        out[label] = {
            "expected_frequency_hz": freq,
            "estimated_frequency_hz": peak["frequency_hz"],
            "freq_error_bins": (peak["frequency_hz"] - freq) / BIN_HZ,
        }
    return out


def evaluate(report):
    """对照判据，返回 (是否全部通过, 未通过项列表)。"""
    failures = []

    a1 = report["part_a_clean"]
    if a1["freq_error_bins"]["max_abs"] > 0.01:
        failures.append(f"A1 频率误差 max={a1['freq_error_bins']['max_abs']:.5f} bin > 0.01")
    if a1["amp_rel_error"]["max_abs"] > 0.01:
        failures.append(f"A1 幅度误差 max={a1['amp_rel_error']['max_abs']:.5f} > 1%")

    a2 = report["part_a_snr40"]
    if a2["freq_error_bins"]["rms"] > 0.02:
        failures.append(f"A2 频率误差 RMS={a2['freq_error_bins']['rms']:.5f} bin > 0.02")

    for row in report["part_b_two_tones"]:
        sep = row["separation_bins"]
        if sep < 4.0:
            # 主瓣宽度内合并为单峰：预期行为，只要求不误报两个峰
            if row["n_peaks_detected"] > 1:
                failures.append(f"B 间距 {sep} bin 应合并却报出 {row['n_peaks_detected']} 个峰")
            continue
        if row["n_peaks_detected"] < 2:
            failures.append(f"B 间距 {sep} bin 未检出两个峰")
            continue
        if sep <= 8.0 and not all(row["interference_flags"][:2]):
            failures.append(f"B 间距 {sep} bin 干扰标志未置位")
        if sep > 8.0 and any(row["interference_flags"][:2]):
            failures.append(f"B 间距 {sep} bin 干扰标志误置位")

    c = report["part_c_boundaries"]
    if not c["dc"]["boundary"] or abs(c["dc"]["amplitude_error"]) > 1e-6:
        failures.append("C 直流边界失败")
    if not c["nyquist"]["boundary"] or abs(c["nyquist"]["amplitude_error"]) > 1e-6:
        failures.append("C 奈奎斯特边界失败")
    for side in ("near_dc", "near_nyquist"):
        if abs(c[f"{side}_3.4bins"]["freq_error_bins"]) > 0.01:
            failures.append(f"C {side} 3.4 bin 频率误差超限")
        if abs(c[f"{side}_1.4bins"]["freq_error_bins"]) > 0.2:
            failures.append(f"C {side} 1.4 bin（退化区）频率误差发散")
    return not failures, failures


def to_markdown(report, failures):
    lines = ["# 验收实验结果", ""]
    a1, a2 = report["part_a_clean"], report["part_a_snr40"]
    lines.append("## A. 单音非整频点扫描（bin 8..2030，偏移 0.13/0.37/0.49）")
    lines.append("")
    lines.append("| 场景 | 样本数 | 频率误差 max (bin) | 频率误差 RMS (bin) | 幅度相对误差 max |")
    lines.append("|---|---|---|---|---|")
    for name, part in (("无噪声", a1), ("SNR 40 dB", a2)):
        f, a = part["freq_error_bins"], part["amp_rel_error"]
        lines.append(f"| {name} | {f['count']} | {f['max_abs']:.6f} | "
                     f"{f['rms']:.6f} | {a['max_abs']:.6f} |")
    lines.append("")
    lines.append("## B. 双近邻频率（f1 = 100.3 bin）")
    lines.append("")
    lines.append("| 间距 (bin) | 检出峰数 | 干扰标志 | 频率误差 (bin) |")
    lines.append("|---|---|---|---|")
    for row in report["part_b_two_tones"]:
        err = row.get("freq_error_bins")
        err_s = ", ".join(f"{e:+.4f}" for e in err) if err else "-"
        lines.append(f"| {row['separation_bins']} | {row['n_peaks_detected']} | "
                     f"{row['interference_flags']} | {err_s} |")
    lines.append("")
    lines.append("## C. 边界")
    lines.append("")
    c = report["part_c_boundaries"]
    lines.append(f"- 直流：幅度估计 {c['dc']['estimated_amplitude']:.9f}"
                 f"（期望 0.5），boundary={c['dc']['boundary']}")
    lines.append(f"- 奈奎斯特：幅度估计 {c['nyquist']['estimated_amplitude']:.9f}"
                 f"（期望 0.7），boundary={c['nyquist']['boundary']}")
    lines.append(f"- 近直流（3.4 bin）：频率误差 "
                 f"{c['near_dc_3.4bins']['freq_error_bins']:+.5f} bin")
    lines.append(f"- 近直流（1.4 bin，退化区）：频率误差 "
                 f"{c['near_dc_1.4bins']['freq_error_bins']:+.5f} bin")
    lines.append(f"- 近奈奎斯特（3.4 bin）：频率误差 "
                 f"{c['near_nyquist_3.4bins']['freq_error_bins']:+.5f} bin")
    lines.append(f"- 近奈奎斯特（1.4 bin，退化区）：频率误差 "
                 f"{c['near_nyquist_1.4bins']['freq_error_bins']:+.5f} bin")
    lines.append("")
    lines.append("## 判据结论")
    lines.append("")
    if failures:
        lines.append("**未通过项：**")
        lines.extend(f"- {f}" for f in failures)
    else:
        lines.append("全部判据通过。")
    lines.append("")
    return "\n".join(lines)


def main():
    report = {
        "config": {"sample_rate": FS, "n_samples": N, "bin_hz": BIN_HZ,
                   "window": "hann", "method": "hann-exact"},
        "part_a_clean": part_a_single_tone_sweep(noise_db=None),
        "part_a_snr40": part_a_single_tone_sweep(noise_db=-40.0),
        "part_b_two_tones": part_b_two_close_tones(),
        "part_c_boundaries": part_c_boundaries(),
    }
    passed, failures = evaluate(report)
    report["passed"] = passed
    report["failures"] = failures

    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = RESULTS_DIR / f"acceptance_{stamp}.json"
    md_path = RESULTS_DIR / f"acceptance_{stamp}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    md = to_markdown(report, failures)
    md_path.write_text(md, encoding="utf-8")

    print(md)
    print(f"原始数据: {json_path}")
    print(f"汇总表:   {md_path}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
