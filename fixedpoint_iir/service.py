"""离线信号处理服务：读取 JSON 请求，执行定点/浮点滤波，输出数值与文件。

用法：
    python -m fixedpoint_iir.service request.json --outdir out/

请求 JSON 字段见 README 与 examples/ 目录。服务只产出数值结果与文件，
不包含任何播放器或图形界面。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

from .analysis import (
    detect_limit_cycle,
    freq_response_error,
    signal_metrics,
    summarize_overflows,
)
from .quantization import QuantSpec
from .signals import build_signal, write_pcm
from .sos_filter import FixedPointSOSFilter, sos_filter_float
from .stability import check_sos_stability, quantize_sos_checked


def _spec_from_dict(d: dict, default_word: int, default_frac: int) -> QuantSpec:
    return QuantSpec(
        word_bits=int(d.get("word_bits", default_word)),
        frac_bits=int(d.get("frac_bits", default_frac)),
        rounding=d.get("rounding", "round"),
        overflow=d.get("overflow", "saturate"),
    )


def run_request(request: dict, outdir: str | Path) -> dict:
    """执行一次滤波请求，写出结果文件并返回报告字典。"""
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    sos = np.asarray(request["sos"], dtype=np.float64)
    coef_spec = _spec_from_dict(request.get("coef_format", {}), 16, 14)
    state_spec = _spec_from_dict(request.get("state_format", {}), 16, 15)
    n_freq = int(request.get("freq_response_points", 512))
    tail = int(request.get("limit_cycle_tail", 64))

    # 1. 输入信号
    x = build_signal(request["signal"])

    # 2. 稳定性检测（基于量化后系数）
    stability = check_sos_stability(sos, coef_spec)
    sos_q, _ = quantize_sos_checked(sos, coef_spec)

    # 3. 浮点参考路径
    y_ref = sos_filter_float(sos, x)

    # 4. 定点路径
    fp = FixedPointSOSFilter(sos, coef_spec, state_spec)
    fixed = fp.process(x)

    # 5. 分析
    freq_err = freq_response_error(sos, sos_q, n_freq)
    overflow = summarize_overflows(fixed)
    metrics = signal_metrics(y_ref, fixed.y)
    limit_cycle = detect_limit_cycle(fixed.y, tail=tail)

    # 6. 写文件
    np.save(outdir / "input.npy", x)
    np.save(outdir / "output_float.npy", y_ref)
    np.save(outdir / "output_fixed.npy", fixed.y)
    outputs = request.get("outputs", {})
    if outputs.get("write_pcm", True):
        write_pcm(str(outdir / "output_fixed.pcm"), fixed.y, outputs.get("pcm_fmt", "s16le"))

    report = {
        "name": request.get("name", "unnamed"),
        "config": {
            "coef_format": coef_spec.describe(),
            "state_format": state_spec.describe(),
            "n_sections": int(sos.shape[0]),
            "n_samples": int(x.shape[0]),
        },
        "sos_quantized": sos_q.tolist(),
        "stability": stability.to_dict(),
        "overflow": overflow,
        "freq_response_error": freq_err,
        "time_domain_metrics": metrics,
        "limit_cycle": limit_cycle,
        "files": {
            "input": "input.npy",
            "output_float": "output_float.npy",
            "output_fixed": "output_fixed.npy",
            "output_fixed_pcm": "output_fixed.pcm" if outputs.get("write_pcm", True) else None,
        },
    }
    with open(outdir / "report.json", "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return report


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description="定点 IIR 离线滤波服务")
    parser.add_argument("request", help="请求 JSON 文件路径")
    parser.add_argument("--outdir", default="out", help="输出目录（默认 out/）")
    args = parser.parse_args(argv)

    with open(args.request, "r", encoding="utf-8") as f:
        request = json.load(f)

    report = run_request(request, args.outdir)

    # 终端摘要（纯文本，无界面）
    print(f"[{report['name']}] 完成，输出目录: {args.outdir}")
    print(f"  稳定性: stable={report['stability']['stable']}, "
          f"风险告警 {len(report['stability']['warnings'])} 条")
    for w in report["stability"]["warnings"]:
        print(f"    ! {w}")
    print(f"  溢出: 总计 {report['overflow']['total_overflows']} 次 "
          f"(输入 {report['overflow']['input_overflows']})")
    fe = report["freq_response_error"]
    print(f"  频响误差: max {fe['max_db_error']:.4f} dB, mean {fe['mean_db_error']:.4f} dB")
    tm = report["time_domain_metrics"]
    print(f"  时域误差: max_abs {tm['max_abs_error']:.6g}, SNR {tm['snr_db']:.2f} dB")
    lc = report["limit_cycle"]
    print(f"  极限环: {lc['has_limit_cycle']} (period={lc['period']}, "
          f"tail_amp={lc['tail_amplitude']:.6g})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
