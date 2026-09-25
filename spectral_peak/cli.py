"""命令行入口：合成信号或本地 PCM -> 分析 -> JSON 数值结果（stdout 或文件）。

用法示例：
    python -m spectral_peak synth --freq 449.9 --fs 48000 --n 4096
    python -m spectral_peak synth --freq 440.0 --freq 486.7 --fs 48000 --n 4096
    python -m spectral_peak pcm --path data.raw --dtype int16 --fs 48000
    python -m spectral_peak run --request examples/synth_single.json --out result.json
"""

from __future__ import annotations

import argparse
import json
import sys

from .service import analyze, analyze_request, synthesize
from .pcm import load_pcm


def _add_analysis_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--window", default="hann", help="窗函数: hann/rectangular/blackmanharris")
    p.add_argument("--method", default="auto", help="插值方法: auto/hann-exact/log-parabolic/quinn")
    p.add_argument("--max-peaks", type=int, default=10, help="最多输出的峰个数")
    p.add_argument("--min-relative-db", type=float, default=-45.0, help="相对最强峰的门限 (dB)")
    p.add_argument("--interference-bins", type=int, default=8, help="邻峰干扰判定距离 (bin)")


def _analysis_kwargs(args: argparse.Namespace) -> dict:
    return {
        "window": args.window,
        "method": args.method,
        "max_peaks": args.max_peaks,
        "min_relative_db": args.min_relative_db,
        "interference_bins": args.interference_bins,
    }


def _emit(result: dict, out: str | None) -> int:
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if out:
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print(f"结果已写入 {out}", file=sys.stderr)
    else:
        print(text)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spectral_peak",
        description="加窗 FFT 峰检测与亚频点插值（纯后端，输出数值 JSON）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_synth = sub.add_parser("synth", help="合成正弦信号并分析")
    p_synth.add_argument("--freq", type=float, action="append", required=True,
                         help="正弦频率 Hz，可重复指定以叠加多音")
    p_synth.add_argument("--amp", type=float, action="append", default=None,
                         help="各正弦幅度，与 --freq 一一对应（缺省 1.0）")
    p_synth.add_argument("--fs", type=float, required=True, help="采样率 Hz")
    p_synth.add_argument("--n", type=int, default=4096, help="样本数")
    p_synth.add_argument("--noise-db", type=float, default=None, help="高斯白噪声 RMS (dB, 满幅=0)")
    p_synth.add_argument("--seed", type=int, default=0, help="噪声随机种子")
    p_synth.add_argument("--out", default=None, help="结果写入文件而非 stdout")
    _add_analysis_args(p_synth)

    p_pcm = sub.add_parser("pcm", help="读取本地 PCM 文件并分析")
    p_pcm.add_argument("--path", required=True, help="PCM 文件路径")
    p_pcm.add_argument("--dtype", default="int16", help="int16/int32/uint8/float32/float64")
    p_pcm.add_argument("--fs", type=float, required=True, help="采样率 Hz")
    p_pcm.add_argument("--n", type=int, default=None, help="最多读取样本数")
    p_pcm.add_argument("--offset", type=int, default=0, help="跳过的起始样本数")
    p_pcm.add_argument("--out", default=None, help="结果写入文件而非 stdout")
    _add_analysis_args(p_pcm)

    p_run = sub.add_parser("run", help="按 JSON 请求文件执行（见 examples/）")
    p_run.add_argument("--request", required=True, help="请求 JSON 文件路径")
    p_run.add_argument("--out", default=None, help="结果写入文件而非 stdout")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "synth":
            freqs = args.freq
            amps = args.amp if args.amp is not None else [1.0] * len(freqs)
            if len(amps) != len(freqs):
                raise ValueError("--amp 数量必须与 --freq 一致")
            tones = [
                {"frequency_hz": f, "amplitude": a} for f, a in zip(freqs, amps)
            ]
            samples = synthesize(tones, args.fs, args.n, args.noise_db, args.seed)
            result = analyze(samples, args.fs, **_analysis_kwargs(args))
        elif args.command == "pcm":
            samples = load_pcm(args.path, dtype=args.dtype,
                               max_samples=args.n, offset_samples=args.offset)
            result = analyze(samples, args.fs, **_analysis_kwargs(args))
        else:  # run
            with open(args.request, "r", encoding="utf-8") as fh:
                request = json.load(fh)
            result = analyze_request(request)
    except (ValueError, FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    return _emit(result, getattr(args, "out", None))


if __name__ == "__main__":
    sys.exit(main())
