"""pcmval 命令行入口(离线,纯文本/JSON 输出,无界面)。

用法示例见 examples/requests/。

    python -m pcmval.cli validate input.wav [--strict]
    python -m pcmval.cli validate input.pcm --raw --bits 24 --channels 2 --sample-rate 48000
    python -m pcmval.cli synth --kind fullscale --bits 24 -o out.wav
    python -m pcmval.cli roundtrip --kind sine --bits 16 -o rt.wav
    python -m pcmval.cli request req.json        # JSON 对象或对象数组
    cat req.json | python -m pcmval.cli request -
"""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__, service, wavio
from .wavio import WavFormatError


def _print_json(obj) -> None:
    json.dump(obj, sys.stdout, ensure_ascii=False, indent=2, sort_keys=False)
    sys.stdout.write("\n")


def _cmd_validate(args: argparse.Namespace) -> int:
    if args.raw:
        req = {
            "action": "validate",
            "container": "raw",
            "path": args.path,
            "bits": args.bits,
            "channels": args.channels,
            "sample_rate": args.sample_rate,
        }
    else:
        req = {"action": "validate", "path": args.path, "strict": args.strict}
    _print_json(service.handle(req))
    return 0


def _cmd_synth(args: argparse.Namespace) -> int:
    req = {
        "action": "synthesize",
        "kind": args.kind,
        "sample_rate": args.sample_rate,
        "duration": args.duration,
        "bits": args.bits,
        "channels": args.channels,
        "amplitude": args.amplitude,
        "out": args.out,
        "raw_out": args.raw_out,
    }
    _print_json(service.handle(req))
    return 0


def _cmd_roundtrip(args: argparse.Namespace) -> int:
    req = {
        "action": "roundtrip",
        "sample_rate": args.sample_rate,
        "duration": args.duration,
        "bits": args.bits,
        "channels": args.channels,
        "amplitude": args.amplitude,
        "kind": args.kind,
        "path": args.path,
        "out": args.out,
    }
    _print_json(service.handle(req))
    return 0


def _cmd_request(args: argparse.Namespace) -> int:
    if args.path == "-":
        payload = sys.stdin.read()
    else:
        with open(args.path, "r", encoding="utf-8") as fh:
            payload = fh.read()
    data = json.loads(payload)
    requests = data if isinstance(data, list) else [data]

    results = []
    exit_code = 0
    for i, req in enumerate(requests):
        try:
            results.append({"index": i, "ok": True, "result": service.handle(req)})
        except (service.BadRequestError, WavFormatError, ValueError, OSError) as exc:
            exit_code = 1
            results.append({"index": i, "ok": False,
                            "error": f"{type(exc).__name__}: {exc}"})
    _print_json(results if isinstance(data, list) else results[0])
    return exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m pcmval.cli",
        description="PCM 容器校验离线服务:WAV 读写、未知 chunk 跳过、"
        "奇数字节补齐、RIFF 长度检查、16/24 bit 整数 PCM 转换。",
    )
    parser.add_argument("--version", action="version", version=f"pcmval {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_val = sub.add_parser("validate", help="校验 WAV 或 raw PCM 文件")
    p_val.add_argument("path")
    p_val.add_argument("--raw", action="store_true", help="输入为无容器 raw PCM")
    p_val.add_argument("--bits", type=int, default=16, choices=(16, 24))
    p_val.add_argument("--channels", type=int, default=1)
    p_val.add_argument("--sample-rate", type=int, default=0)
    p_val.add_argument("--strict", action="store_true", help="warning 也视为失败")
    p_val.set_defaults(func=_cmd_validate)

    p_syn = sub.add_parser("synth", help="合成信号并写出 WAV/raw PCM")
    p_syn.add_argument("--kind", default="sine",
                       choices=("sine", "silence", "fullscale", "ramp"))
    p_syn.add_argument("--sample-rate", type=int, default=8000)
    p_syn.add_argument("--duration", type=float, default=0.01)
    p_syn.add_argument("--bits", type=int, default=16, choices=(16, 24))
    p_syn.add_argument("--channels", type=int, default=1)
    p_syn.add_argument("--amplitude", type=float, default=0.5)
    p_syn.add_argument("-o", "--out", default=None, help="输出 WAV 路径")
    p_syn.add_argument("--raw-out", default=None, help="同时输出 raw PCM 路径")
    p_syn.set_defaults(func=_cmd_synth)

    p_rt = sub.add_parser("roundtrip", help="WAV 写出+读回的字节/振幅往返验证")
    p_rt.add_argument("--path", default=None, help="用已有 WAV 作为源(默认用合成信号)")
    p_rt.add_argument("--kind", default="fullscale",
                      choices=("sine", "silence", "fullscale", "ramp"))
    p_rt.add_argument("--sample-rate", type=int, default=8000)
    p_rt.add_argument("--duration", type=float, default=0.01)
    p_rt.add_argument("--bits", type=int, default=16, choices=(16, 24))
    p_rt.add_argument("--channels", type=int, default=1)
    p_rt.add_argument("--amplitude", type=float, default=0.5)
    p_rt.add_argument("-o", "--out", default=None)
    p_rt.set_defaults(func=_cmd_roundtrip)

    p_req = sub.add_parser("request", help="执行 JSON 请求(对象或数组),'-' 表示 stdin")
    p_req.add_argument("path")
    p_req.set_defaults(func=_cmd_request)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (service.BadRequestError, WavFormatError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"error: file not found: {exc.filename}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
