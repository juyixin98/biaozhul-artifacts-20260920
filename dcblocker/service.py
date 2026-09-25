"""离线去偏置服务：读入 JSON 请求，输出数值指标与处理后文件。

请求（JSON）示例见 requests/ 目录：

{
  "sample_rate": 48000,
  "cutoff_hz": 5.0,
  "block_size": 1024,
  "input":  {"type": "sine", "frequency": 440, "amplitude": 0.5,
             "dc_offset": 0.3, "duration_s": 2.0},
  "output_file": "out/cleaned.f32"
}

input 也可以是本地 PCM 文件：
  {"type": "pcm_file", "path": "in/raw.i16", "format": "int16"}

响应（JSON，打印到 stdout）：输入/输出统计、稳态残差、输出文件路径等。
所有路径相对当前工作目录解析。
"""

from __future__ import annotations

import json
import math
import os

import numpy as np

from .filter import DCBlocker
from .pcm import format_for_extension, read_pcm, write_pcm
from .signals import generate

_TAIL_RATIO = 0.1  # 末尾 10% 作为稳态评估窗口


def _load_input(spec: dict, sample_rate: float) -> np.ndarray:
    kind = spec.get("type")
    if kind == "pcm_file":
        path = spec["path"]
        fmt = spec.get("format") or format_for_extension(path)
        return read_pcm(path, fmt)
    return generate(spec, sample_rate)


def _stats(x: np.ndarray) -> dict:
    if x.size == 0:
        return {"mean": 0.0, "std": 0.0, "max_abs": 0.0}
    return {
        "mean": float(np.mean(x)),
        "std": float(np.std(x)),
        "max_abs": float(np.max(np.abs(x))),
    }


def run_request(request: dict) -> dict:
    """执行一次去偏置请求，返回指标字典（纯数值，可 JSON 序列化）。"""
    sample_rate = float(request["sample_rate"])
    cutoff_hz = float(request.get("cutoff_hz", 5.0))
    block_size = int(request.get("block_size", 1024))
    if block_size <= 0:
        raise ValueError(f"block_size 必须为正整数，得到 {block_size!r}")

    x = _load_input(request["input"], sample_rate)

    blocker = DCBlocker(sample_rate=sample_rate, cutoff_hz=cutoff_hz)
    blocks = [
        blocker.process(x[i : i + block_size])
        for i in range(0, x.size, block_size)
    ]
    y = np.concatenate(blocks) if blocks else np.empty(0, dtype=np.float64)

    output_file = request.get("output_file")
    if output_file:
        directory = os.path.dirname(output_file)
        if directory:
            os.makedirs(directory, exist_ok=True)
        fmt = request.get("output_format") or format_for_extension(output_file)
        write_pcm(output_file, y, fmt)

    tail = y[max(0, y.size - int(math.ceil(y.size * _TAIL_RATIO))) :]
    r = blocker.coefficient
    settle_1pct_s = math.log(0.01) / math.log(r) / sample_rate  # 理论 1% 稳定时间

    return {
        "sample_rate": sample_rate,
        "cutoff_hz": cutoff_hz,
        "block_size": block_size,
        "coefficient_r": r,
        "theoretical_settle_1pct_s": settle_1pct_s,
        "samples": int(x.size),
        "input": _stats(x),
        "output": _stats(y),
        "steady_state_tail": _stats(tail),
        "output_file": output_file,
    }


def run_request_file(request_path: str) -> dict:
    """从 JSON 文件加载请求并执行。"""
    with open(request_path, "r", encoding="utf-8") as fh:
        request = json.load(fh)
    return run_request(request)
