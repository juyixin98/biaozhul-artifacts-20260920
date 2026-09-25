"""命令行入口：离线重采样服务。

用法：
    python -m rational_resampler.cli --job examples/request_sine_3_2.json
    python -m rational_resampler.cli --signal sine --freq 1000 --duration 0.1 \\
        --fs-in 48000 --up 3 --down 2 --output out.wav --output-format wav
"""

from __future__ import annotations

import argparse
import json
import sys

from .pcm_io import PCM_FORMATS
from .service import run_job


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rational-resampler",
        description="离线有理比 (L/M) 重采样服务：输入合成信号或本地 PCM，输出数值报告与文件。",
    )
    p.add_argument("--job", help="JSON 请求文件路径（给定后忽略其余参数）")
    p.add_argument("--up", type=int, help="上采样因子 L")
    p.add_argument("--down", type=int, help="下采样因子 M")
    p.add_argument("--fs-in", type=float, default=48000.0, help="输入采样率 Hz")
    p.add_argument("--signal", choices=["sine", "impulse", "noise", "multitone"],
                   help="合成信号类型（不给则从 --input 读取）")
    p.add_argument("--freq", type=float, help="正弦频率 Hz")
    p.add_argument("--duration", type=float, default=0.1, help="合成信号时长 s")
    p.add_argument("--n", type=int, default=512, help="impulse/noise 样本数")
    p.add_argument("--input", help="输入 PCM/WAV 文件路径")
    p.add_argument("--input-format", default="s16le", choices=PCM_FORMATS)
    p.add_argument("--output", help="输出文件路径")
    p.add_argument("--output-format", choices=PCM_FORMATS, help="默认同输入格式")
    p.add_argument("--pad-mode", default="edge",
                   choices=["zero", "edge", "reflect", "none"])
    p.add_argument("--attenuation-db", type=float, default=80.0)
    p.add_argument("--block-size", type=int, default=0, help="分块大小（0=整段）")
    p.add_argument("--report", help="报告 JSON 写出路径")
    return p


def _job_from_args(args: argparse.Namespace) -> dict:
    if args.up is None or args.down is None:
        raise SystemExit("error: --up and --down are required")
    job: dict = {
        "up": args.up,
        "down": args.down,
        "fs_in": args.fs_in,
        "pad_mode": args.pad_mode,
        "attenuation_db": args.attenuation_db,
        "block_size": args.block_size,
    }
    if args.signal:
        sig: dict = {"type": args.signal, "fs": args.fs_in}
        if args.signal == "sine":
            if args.freq is None:
                raise SystemExit("error: --freq is required for sine")
            sig.update(freq=args.freq, duration=args.duration)
        elif args.signal in ("impulse", "noise"):
            sig.update(n=args.n)
        job["signal"] = sig
    elif args.input:
        job["input"] = args.input
        job["input_format"] = args.input_format
    else:
        raise SystemExit("error: provide --signal or --input")
    if args.output:
        job["output"] = args.output
    if args.output_format:
        job["output_format"] = args.output_format
    if args.report:
        job["report"] = args.report
    return job


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.job:
        with open(args.job, "r", encoding="utf-8") as f:
            job = json.load(f)
    else:
        job = _job_from_args(args)
    report = run_job(job)
    json.dump(report, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
