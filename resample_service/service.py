"""离线重采样服务：读取 JSON 请求文件，输出数值文件与 JSON 报告。

用法：
    python -m resample_service.service request.json [request2.json ...]

请求格式见 README 与 examples/ 目录。服务只产生数值与文件，无界面。
"""

from __future__ import annotations

import json
import sys
from fractions import Fraction
from pathlib import Path

import numpy as np

from . import synth
from .pcm_io import read_pcm, write_pcm
from .resampler import RationalResampler


def _build_input(spec: dict, base_dir: Path) -> tuple[np.ndarray, float]:
    """根据请求构造输入信号，返回 (x, fs_in)。"""
    kind = spec.get("type")
    fs = float(spec["sample_rate"])
    if kind == "sine":
        x = synth.sine(fs, spec["frequency"], spec["duration"],
                       spec.get("amplitude", 1.0), spec.get("phase", 0.0))
    elif kind == "impulse":
        n = int(round(fs * spec["duration"]))
        x = synth.impulse(n, spec.get("index"), spec.get("amplitude", 1.0))
    elif kind == "multitone":
        x = synth.multitone(fs, spec["components"], spec["duration"])
    elif kind == "chirp":
        x = synth.chirp(fs, spec["f0"], spec["f1"], spec["duration"],
                        spec.get("amplitude", 1.0))
    elif kind == "noise":
        n = int(round(fs * spec["duration"]))
        x = synth.noise(n, spec.get("seed", 0), spec.get("amplitude", 1.0))
    elif kind == "pcm_file":
        path = base_dir / spec["path"]
        x = read_pcm(str(path), spec.get("format", "s16le"))
    else:
        raise ValueError(f"未知输入类型 {kind!r}")
    return x, fs


def _resolve_ratio(resample_spec: dict, fs_in: float) -> tuple[int, int, float]:
    """返回 (up, down, fs_out)。"""
    if "up" in resample_spec and "down" in resample_spec:
        up, down = int(resample_spec["up"]), int(resample_spec["down"])
    elif "target_sample_rate" in resample_spec:
        frac = Fraction(resample_spec["target_sample_rate"]).limit_denominator(10_000) \
               / Fraction(fs_in).limit_denominator(10_000)
        up, down = frac.numerator, frac.denominator
    else:
        raise ValueError("resample 需要 up/down 或 target_sample_rate")
    return up, down, fs_in * up / down


def run_request(request: dict, base_dir: str | Path = ".") -> dict:
    """执行一个请求，写输出文件与报告，返回报告 dict。"""
    base_dir = Path(base_dir)
    x, fs_in = _build_input(request["input"], base_dir)

    filt = request.get("filter", {})
    boundary = request.get("boundary", {})
    up, down, fs_out = _resolve_ratio(request["resample"], fs_in)

    rs = RationalResampler(
        up, down,
        atten_db=filt.get("atten_db", 80.0),
        transition=filt.get("transition", 0.1),
        num_taps=filt.get("num_taps"),
        pad_mode=boundary.get("pad_mode", "zero"),
    )

    block_size = int(request.get("processing", {}).get("block_size", 0))
    if block_size > 0:
        y = rs.process_blocks(x, block_size)
        mode = f"blocks({block_size})"
    else:
        y = rs.process(x)
        mode = "whole"

    out_spec = request["output"]
    out_path = base_dir / out_spec["path"]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fmt = out_spec.get("format", "f64le")
    write_pcm(str(out_path), y, fmt)

    report = {
        "input": {"type": request["input"].get("type"),
                  "sample_rate": fs_in, "length": int(len(x))},
        "resample": {"up": rs.up, "down": rs.down,
                     "input_sample_rate": fs_in,
                     "output_sample_rate": fs_out},
        "filter": {"num_taps": rs.num_taps,
                   "cutoff_cycles_per_upsampled_sample": rs.cutoff,
                   "atten_db": rs.atten_db,
                   "transition": rs.transition},
        "boundary": {"pad_mode": rs.pad_mode},
        "group_delay": {"upsampled_samples": rs.group_delay_up,
                        "input_samples": rs.group_delay_in,
                        "seconds": rs.group_delay_seconds(fs_in)},
        "processing": {"mode": mode},
        "output": {"path": str(out_path), "format": fmt,
                   "length": int(len(y)),
                   "expected_length": rs.output_length(len(x))},
    }
    report["ok"] = report["output"]["length"] == report["output"]["expected_length"]

    report_path = out_spec.get("report_path")
    if report_path:
        rp = base_dir / report_path
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    return report


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    status = 0
    for req_path in argv:
        req_path = Path(req_path)
        request = json.loads(req_path.read_text())
        report = run_request(request, base_dir=req_path.parent)
        line = (f"[{req_path.name}] {report['input']['length']} -> "
                f"{report['output']['length']} 样本 "
                f"(L/M={report['resample']['up']}/{report['resample']['down']}, "
                f"{report['resample']['input_sample_rate']} Hz -> "
                f"{report['resample']['output_sample_rate']} Hz, "
                f"{report['processing']['mode']}) -> {report['output']['path']}")
        print(line)
        if not report["ok"]:
            status = 1
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
