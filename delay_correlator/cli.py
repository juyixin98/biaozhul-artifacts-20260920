"""命令行入口（无服务端口、无界面）：读请求 JSON，打印结果 JSON。

用法
----
    python -m delay_correlator run request.json [-o result.json]
    python -m delay_correlator demo --case positive-delay
    python -m delay_correlator --help

文件类输入（PCM/WAV）相对于请求 JSON 所在目录解析；输出文件同理。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .service import load_request, run_request, save_json

DEMO_CASES = {
    "positive-delay": {
        "input": {
            "type": "synthetic",
            "signal_type": "noise",
            "n": 8000,
            "sample_rate": 8000,
            "delay": 17,
            "snr_db": 20,
            "seed": 42,
        },
        "analysis": {"max_lag_samples": 100, "method": "zncc"},
    },
    "negative-delay": {
        "input": {
            "type": "synthetic",
            "signal_type": "noise",
            "n": 8000,
            "sample_rate": 8000,
            "delay": -17,
            "snr_db": 20,
            "seed": 42,
        },
        "analysis": {"max_lag_samples": 100, "method": "zncc"},
    },
    "silence": {
        "input": {
            "type": "synthetic",
            "signal_type": "silence",
            "n": 4000,
            "sample_rate": 8000,
            "delay": 5,
        },
        "analysis": {"max_lag_samples": 50},
    },
    "periodic": {
        "input": {
            "type": "synthetic",
            "signal_type": "sine",
            "n": 8000,
            "sample_rate": 8000,
            "freq": 200.0,
            "delay": 20,
        },
        "analysis": {"max_lag_samples": 100, "ambiguity_guard_samples": 3},
    },
}


def _cmd_run(args: argparse.Namespace) -> int:
    req_path = Path(args.request)
    req = load_request(req_path)
    result = run_request(req, base_dir=req_path.parent.resolve())
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        save_json(args.output, result)
        print(f"结果已写入 {args.output}", file=sys.stderr)
    print(text)
    return 0


def _cmd_demo(args: argparse.Namespace) -> int:
    if args.case not in DEMO_CASES:
        print(f"未知样例 {args.case!r}，可选：{', '.join(DEMO_CASES)}",
              file=sys.stderr)
        return 2
    result: dict[str, Any] = run_request(DEMO_CASES[args.case], base_dir=Path.cwd())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="delay_correlator",
        description="双通道归一化互相关延迟估计（纯后端，无界面）",
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="按请求 JSON 执行离线分析")
    p_run.add_argument("request", help="请求 JSON 文件路径")
    p_run.add_argument("-o", "--output", help="可选：结果 JSON 输出路径")
    p_run.set_defaults(func=_cmd_run)

    p_demo = sub.add_parser("demo", help="运行内置演示请求并打印结果")
    p_demo.add_argument(
        "--case",
        choices=sorted(DEMO_CASES),
        default="positive-delay",
        help="演示场景（默认 positive-delay）",
    )
    p_demo.set_defaults(func=_cmd_demo)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
