"""离线重采样服务：接收请求字典，返回数值报告并写出文件。

请求（job）字段：
- up, down        : 有理比 L/M（必填，正整数）
- fs_in           : 输入采样率 Hz（合成信号时可由 signal.fs 提供）
- signal          : 合成信号 spec（见 signals.make_signal），与 input 二选一
- input           : 输入文件路径（与 signal 二选一）
- input_format    : s16le | s32le | f32le | wav（默认 s16le）
- pad_mode        : zero | edge | reflect | none（默认 edge）
- attenuation_db  : 抗混叠 FIR 阻带衰减（默认 80）
- output          : 输出文件路径（可选；不给则只返回数值）
- output_format   : 同 input_format（默认同输入或 s16le）
- report          : 报告 JSON 写出路径（可选）

返回报告 dict（全部为数值与路径，无界面产物）。
"""

from __future__ import annotations

import json

import numpy as np

from .core import StreamingResampler, filter_info
from .filter_design import design_anti_alias_fir
from .metrics import sine_fit, suppression_db
from .pcm_io import read_any, write_any
from .signals import make_signal


def _load_input(job: dict) -> tuple[np.ndarray, float]:
    if "signal" in job and job["signal"] is not None:
        x, fs = make_signal(job["signal"])
        return x, float(job.get("fs_in", fs))
    if "input" in job and job["input"] is not None:
        fmt = job.get("input_format", "s16le")
        x, fs_file = read_any(job["input"], fmt)
        fs = job.get("fs_in", fs_file)
        if fs is None:
            raise ValueError("fs_in is required for raw PCM input")
        return x, float(fs)
    raise ValueError("job must provide either 'signal' or 'input'")


def run_job(job: dict) -> dict:
    """执行一次重采样请求，返回数值报告。"""
    up = int(job["up"])
    down = int(job["down"])
    pad_mode = job.get("pad_mode", "edge")
    att = float(job.get("attenuation_db", 80.0))

    x, fs_in = _load_input(job)
    h = design_anti_alias_fir(up, down, attenuation_db=att)

    # 分块执行（默认单块；job 可指定 block_size 验证分块一致性）
    r = StreamingResampler(up, down, h=h, pad_mode=pad_mode)
    block = int(job.get("block_size", 0)) or x.size or 1
    parts = [r.process(x[i : i + block]) for i in range(0, x.size, block)]
    parts.append(r.finish())
    y = np.concatenate([p for p in parts if p.size]) if parts else np.zeros(0)

    fs_out = fs_in * up / down
    report: dict = {
        "fs_in": fs_in,
        "fs_out": fs_out,
        "ratio": f"{up}/{down}",
        "n_in": int(x.size),
        "n_out": int(y.size),
        "expected_n_out": int(-(-x.size * up // down)) if x.size else 0,
        "pad_mode": pad_mode,
        "pad_left": r.pad_left,
        "pad_right": r.pad_right,
        "block_size": block,
        **filter_info(h, up, down),
    }

    # 合成正弦输入时自动附验收数值：通带拟合误差 / 阻带抑制
    sig = job.get("signal") or {}
    if sig.get("type") == "sine" and y.size:
        freq = float(sig["freq"])
        skip = min(report["pad_left"] * up // down + 8, y.size // 4)
        fit = sine_fit(y, freq, fs_out, skip=skip)
        report["sine_fit"] = fit
        if freq > fs_out / 2.0:
            report["alias_suppression_db"] = suppression_db(
                y, float(sig.get("amplitude", 1.0)), skip=skip
            )

    out_path = job.get("output")
    if out_path:
        fmt = job.get("output_format") or job.get("input_format", "s16le")
        write_any(out_path, y, fmt, fs_out)
        report["output"] = out_path
        report["output_format"] = fmt

    report_path = job.get("report")
    if report_path:
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        report["report"] = report_path

    return report
