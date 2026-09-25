"""离线信号处理服务:读取 JSON 请求,执行流式 FIR 处理,输出数值与文件。

请求格式(见 examples/requests/):
{
  "sample_rate": 48000,
  "input":  {"kind": "synthetic", "signal": {"kind": "mixed", "n": 5000,
             "channels": 2, "seed": 1}}
           | {"kind": "pcm", "path": "in.pcm", "channels": 2, "dtype": "s16le"},
  "filter":  <滤波器规格, 见 filters.design>,
  "chunk_size": 512,                 // 或 "chunks": [512, 100, ...]
  "schedule": [                      // 运行中切换计划(可空)
    {"at": 2000, "filter": <规格>, "fade_len": 256, "channels": [0]}
  ],
  "history_capacity": 8192,
  "output": {"dir": "out", "pcm_dtype": "s16le", "write_input_pcm": false},
  "check":  {"threshold": null}      // 突跳检测阈值;null = 自动
}

返回报告 dict,并写出:
  <out>/output.pcm   处理结果(与输入同通道数,长度 = n + 尾部)
  <out>/report.json  全部数值指标
  <out>/input.pcm    可选,合成输入的落盘副本
"""

from __future__ import annotations

import json
import os

import numpy as np

from . import filters as filter_lib
from . import pcm as pcm_lib
from . import signals as signal_lib
from .discontinuity import detect_boundary_jumps
from .engine import StreamingFIREngine
from .reference import LayeredReferenceFIR


def load_request(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _build_input(req: dict) -> tuple[np.ndarray, int]:
    spec = req["input"]
    sr = int(req.get("sample_rate", 48000))
    if spec["kind"] == "synthetic":
        sig = dict(spec["signal"])
        x = signal_lib.generate_signal(sample_rate=sr, **sig)
        return (x.reshape(-1, 1) if x.ndim == 1 else x), sr
    if spec["kind"] == "pcm":
        path = spec["path"]
        if not os.path.isabs(path):
            path = os.path.join(os.getcwd(), path)
        x = pcm_lib.read_pcm(path, channels=spec.get("channels", 1),
                             dtype=spec.get("dtype", "s16le"))
        return (x.reshape(-1, 1) if x.ndim == 1 else x), sr
    raise ValueError(f"未知输入类型: {spec['kind']!r}")


def _chunk_plan(n: int, req: dict, events: list[int]) -> list[int]:
    """把 [0, n) 切成块;切换事件发生处强制为块边界。"""
    if "chunks" in req:
        sizes = [int(s) for s in req["chunks"]]
        if any(s <= 0 for s in sizes):
            raise ValueError("chunks 中的块长必须为正")
        bounds = set(np.cumsum(sizes).tolist())
    else:
        step = int(req.get("chunk_size", 512))
        if step <= 0:
            raise ValueError("chunk_size 必须为正")
        bounds = set(range(step, n, step))
    bounds |= {e for e in events if 0 < e < n}
    cuts = sorted(b for b in bounds if 0 < b < n)
    plan, prev = [], 0
    for b in cuts + [n]:
        plan.append(b - prev)
        prev = b
    return [s for s in plan if s > 0]


def run_request(req: dict) -> dict:
    sr = int(req.get("sample_rate", 48000))
    x, sr = _build_input(req)
    n, channels = x.shape

    schedule = sorted(req.get("schedule", []), key=lambda e: e["at"])
    events = [int(e["at"]) for e in schedule]
    for e in events:
        if not 0 <= e <= n:
            raise ValueError(f"切换位置 {e} 超出信号长度 {n}")

    coeffs0 = filter_lib.design(req["filter"], sr)
    capacity = int(req.get("history_capacity", 8192))
    engine = StreamingFIREngine(coeffs0, history_capacity=capacity)
    ref = LayeredReferenceFIR(coeffs0, history_capacity=capacity)

    plan = _chunk_plan(n, req, events)
    boundaries = np.cumsum(plan)[:-1].tolist()  # 各块起始位置(不含 0)

    # 逐块处理;事件在其样本位置(必为块边界)前应用
    ev_iter = iter(schedule)
    pending = next(ev_iter, None)
    outs, ref_outs = [], []
    pos = 0
    for size in plan:
        while pending is not None and int(pending["at"]) == pos:
            c = filter_lib.design(pending["filter"], sr)
            fade = int(pending.get("fade_len", 0))
            chs = pending.get("channels")
            engine.switch(c, channels=chs, fade_len=fade)
            ref.switch(c, channels=chs, fade_len=fade)
            pending = next(ev_iter, None)
        blk = x[pos:pos + size]
        outs.append(engine.process(blk))
        ref_outs.append(ref.process(blk))
        pos += size
    if pending is not None:  # at == n:信号末尾的切换,只影响尾部
        c = filter_lib.design(pending["filter"], sr)
        engine.switch(c, channels=pending.get("channels"),
                      fade_len=int(pending.get("fade_len", 0)))
        ref.switch(c, channels=pending.get("channels"),
                   fade_len=int(pending.get("fade_len", 0)))

    tail = engine.drain()
    ref_tail = ref.drain()
    y = np.concatenate(outs + [tail], axis=0) if tail.size else np.concatenate(outs, axis=0)
    y_ref = (np.concatenate(ref_outs + [ref_tail], axis=0)
             if ref_tail.size else np.concatenate(ref_outs, axis=0))

    # ---- 数值指标 ----
    check = req.get("check", {})
    disc = detect_boundary_jumps(y, boundaries,
                                 threshold=check.get("threshold"))
    err = np.abs(y - y_ref)
    report = {
        "sample_rate": sr,
        "n_input_samples": n,
        "n_output_samples": int(y.shape[0]),
        "n_tail_samples": int(y.shape[0] - n),
        "channels": channels,
        "n_blocks": len(plan),
        "block_sizes": plan,
        "block_boundaries": boundaries,
        "n_switches": len(schedule),
        "output_peak": float(np.abs(y).max()) if y.size else 0.0,
        "output_rms_per_channel": [
            float(np.sqrt(np.mean(y[:, ch] ** 2))) for ch in range(y.shape[1])
        ],
        "reference_max_abs_err": float(err.max()) if err.size else 0.0,
        "reference_mean_abs_err": float(err.mean()) if err.size else 0.0,
        "discontinuity": disc.to_dict(),
    }

    # ---- 文件输出 ----
    out_cfg = req.get("output", {})
    out_dir = out_cfg.get("dir", "out")
    os.makedirs(out_dir, exist_ok=True)
    pcm_dtype = out_cfg.get("pcm_dtype", "s16le")
    out_pcm = os.path.join(out_dir, "output.pcm")
    pcm_lib.write_pcm(out_pcm, y, dtype=pcm_dtype)
    report["files"] = {"output_pcm": out_pcm}
    if out_cfg.get("write_input_pcm"):
        in_pcm = os.path.join(out_dir, "input.pcm")
        pcm_lib.write_pcm(in_pcm, x, dtype=pcm_dtype)
        report["files"]["input_pcm"] = in_pcm
    report_path = os.path.join(out_dir, "report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    report["files"]["report_json"] = report_path
    return report
