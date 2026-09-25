"""命令行入口(纯后端,无界面)。

用法:
  python -m fir_stream.cli run REQUEST.json [--out-dir DIR]
  python -m fir_stream.cli make-example-pcm PATH [--seconds 2]
      [--channels 2] [--dtype s16le] [--sample-rate 48000]
"""

from __future__ import annotations

import argparse
import json
import sys

from . import pcm as pcm_lib
from . import signals as signal_lib
from .service import load_request, run_request


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="fir_stream", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="执行 JSON 处理请求")
    p_run.add_argument("request", help="请求 JSON 文件路径")
    p_run.add_argument("--out-dir", default=None,
                       help="覆盖请求中的 output.dir")

    p_pcm = sub.add_parser("make-example-pcm", help="生成示例 PCM 输入文件")
    p_pcm.add_argument("path")
    p_pcm.add_argument("--seconds", type=float, default=2.0)
    p_pcm.add_argument("--channels", type=int, default=2)
    p_pcm.add_argument("--dtype", default="s16le")
    p_pcm.add_argument("--sample-rate", type=int, default=48000)
    p_pcm.add_argument("--seed", type=int, default=7)

    args = parser.parse_args(argv)

    if args.cmd == "run":
        req = load_request(args.request)
        if args.out_dir:
            req.setdefault("output", {})["dir"] = args.out_dir
        report = run_request(req)
        summary = {
            "n_input_samples": report["n_input_samples"],
            "n_output_samples": report["n_output_samples"],
            "n_blocks": report["n_blocks"],
            "n_switches": report["n_switches"],
            "reference_max_abs_err": report["reference_max_abs_err"],
            "discontinuity_ok": report["discontinuity"]["ok"],
            "boundary_max_abs_diff": report["discontinuity"]["boundary_max_abs_diff"],
            "files": report["files"],
        }
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0 if report["discontinuity"]["ok"] else 1

    if args.cmd == "make-example-pcm":
        n = int(args.seconds * args.sample_rate)
        x = signal_lib.generate_signal(
            "mixed", n=n, channels=args.channels,
            sample_rate=args.sample_rate, seed=args.seed)
        pcm_lib.write_pcm(args.path, x, dtype=args.dtype)
        print(f"已写出 {args.path}: {n} 帧 x {args.channels} 通道, {args.dtype}")
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
