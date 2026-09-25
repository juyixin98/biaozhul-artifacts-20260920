"""命令行：从文件或标准输入读取 JSON 请求，输出 JSON 响应。

用法::

    python -m kfmu.cli --input examples/request_example.json
    cat request.json | python -m kfmu.cli > response.json

退出码：
  0  请求合法并完成运行（逐步错误记录在响应中）；
  2  请求/模型非法（致命），输出错误信封；
  3  加 --strict-step-errors 且存在 status="error" 的步。
"""

from __future__ import annotations

import argparse
import json
import sys

from .errors import KalmanError
from .jsonio import error_envelope, process_request


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m kfmu.cli",
        description="线性卡尔曼滤波（支持部分观测缺测）JSON 接口",
    )
    parser.add_argument("-i", "--input", default="-",
                        help="请求 JSON 文件路径，- 表示标准输入（默认）")
    parser.add_argument("-o", "--output", default="-",
                        help="响应 JSON 文件路径，- 表示标准输出（默认）")
    parser.add_argument("--strict-step-errors", action="store_true",
                        help="任一步 status=error 时以退出码 3 结束")
    args = parser.parse_args(argv)

    try:
        raw = sys.stdin.read() if args.input == "-" else open(args.input, "r", encoding="utf-8").read()
    except OSError as exc:
        return _emit_fatal(args.output, {"ok": False, "error": {
            "code": "io_error", "message": f"读取输入失败：{exc}"}})

    try:
        payload = json.loads(raw, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        return _emit_fatal(args.output, error_envelope(
            KalmanError(f"JSON 解析失败：{exc}", code="invalid_json")))

    try:
        response = process_request(payload)
    except KalmanError as exc:
        return _emit_fatal(args.output, error_envelope(exc))

    text = json.dumps(response, ensure_ascii=False, indent=2, allow_nan=False)
    _write_text(args.output, text)

    if args.strict_step_errors and response["summary"]["num_errors"] > 0:
        return 3
    return 0


def _reject_constant(value: str):
    raise ValueError(f"非法 JSON 常量：{value}（不允许 NaN/Infinity，缺测用 null）")


def _emit_fatal(out_path: str, envelope: dict) -> int:
    _write_text(out_path, json.dumps(envelope, ensure_ascii=False, indent=2))
    return 2


def _write_text(out_path: str, text: str) -> None:
    if out_path == "-":
        sys.stdout.write(text + "\n")
    else:
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
