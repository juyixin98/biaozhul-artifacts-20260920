"""命令行入口（纯文本输出，不含任何播放器/界面）。

用法::

    python -m delay_correlator run examples/request_synthetic.json -o out/synth
    python -m delay_correlator run examples/request_pcm.json       -o out/pcm

``run`` 读取请求 JSON，解析其中相对请求文件目录的输入路径，执行离线分析，
写出 results.json / correlation.npz / signals.npz，并在标准输出打印简要文本汇总。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .pipeline import run_analysis


def _print_summary(result: dict) -> None:
    src = result["source"]
    s = result["summary"]
    print("=== 延迟估计结果汇总 ===")
    print(f"输入模式      : {src['mode']} ({src.get('kind', '-')})")
    if src["mode"] == "synthetic":
        print(f"信号类型      : {src['kind']}")
        print(f"真值延迟      : {src['delay_samples_truth']} samples")
    print(f"采样率        : {src.get('sample_rate')} Hz")
    print(
        f"窗口          : 共 {s['n_windows']}，可信 {s['n_confident']}，"
        f"不确定 {s['n_uncertain']}；一致性: {s['consensus']}"
    )
    mean = s["delay_samples_mean"]
    if mean is not None:
        sec = s["delay_seconds_mean"]
        print(f"平均延迟      : {mean:.3f} samples" + (f" ({sec:.6f} s)" if sec is not None else ""))
        print(f"窗口间散布    : {s['delay_samples_spread']:.3f} samples")
    else:
        print("平均延迟      : 不可用（无可信窗口）")
    print("逐窗明细      :")
    for w in result["windows"]:
        extra = "" if w["status"] == "confident" else f"  原因={','.join(w['reasons'])}"
        delay = w["delay_samples"]
        delay_txt = "null" if delay is None else f"{delay:8.3f}"
        print(
            f"  #{w['window']:>2} [{w['start_sample']:>6}:{w['end_sample']:>6}] "
            f"delay={delay_txt}  peak={_fmt(w['peak'])}  "
            f"ratio={_fmt(w['peak_ratio'])}  rms_db={_fmt(w['rms_db'])}  "
            f"{w['status']}{extra}"
        )
    files = result.get("_output_files", {})
    if files:
        print("输出文件      :")
        for key in ("results_json", "correlation_npz", "signals_npz"):
            if key in files:
                print(f"  {files[key]}")


def _fmt(x: float | None) -> str:
    return "  null" if x is None else f"{x:6.3f}"


def _cmd_run(args: argparse.Namespace) -> int:
    req_path = Path(args.request).resolve()
    if not req_path.is_file():
        print(f"错误: 请求文件不存在: {req_path}", file=sys.stderr)
        return 2
    try:
        request = json.loads(req_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"错误: 请求 JSON 解析失败: {e}", file=sys.stderr)
        return 2

    if args.output:
        out_dir = Path(args.output).resolve()
    else:
        out_dir = (Path.cwd() / (req_path.stem + "_out")).resolve()
    try:
        result = run_analysis(request, base_dir=req_path.parent, output_dir=out_dir)
    except (ValueError, FileNotFoundError, FileExistsError) as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1

    _print_summary(result)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="delay_correlator",
        description="基于归一化互相关的双通道延迟估计离线服务（纯后端）",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="执行一次延迟估计分析")
    p_run.add_argument("request", help="请求 JSON 文件路径")
    p_run.add_argument(
        "-o", "--output", default=None,
        help="输出目录（默认 <请求名>_out）；已存在结果文件时拒绝覆盖",
    )
    p_run.set_defaults(func=_cmd_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
