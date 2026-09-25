"""命令行 JSON 接口。

用法::

    python -m mcf.cli request.json            # 从文件读
    cat request.json | python -m mcf.cli      # 从标准输入读
    python -m mcf.cli -                       # 显式标准输入

成功时把响应 JSON 打印到标准输出并以 0 退出；
请求/业务错误以退出码 2 结束；内部错误以退出码 1 结束。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import TextIO

from .api import MCFError, error_response, solve_request


def _read_input(path: str) -> str:
    if path in ("-", ""):
        return sys.stdin.read()
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def run(stream_in: TextIO, stream_out: TextIO, path: str | None) -> int:
    text = stream_in.read() if path is None else _read_input(path)

    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        response = error_response("invalid_json", f"JSON 解析失败: {exc}")
        print(json.dumps(response, ensure_ascii=False, indent=2), file=stream_out)
        return 2

    try:
        response = solve_request(payload)
    except MCFError as exc:
        response = error_response(exc.code, exc.message)
        print(json.dumps(response, ensure_ascii=False, indent=2), file=stream_out)
        return 2

    print(json.dumps(response, ensure_ascii=False, indent=2), file=stream_out)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="整数容量/费用的最小费用最大流 JSON 接口"
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=None,
        help="请求 JSON 文件路径；省略或为 '-' 时从标准输入读取",
    )
    args = parser.parse_args(argv)
    return run(sys.stdin, sys.stdout, args.path)


if __name__ == "__main__":
    sys.exit(main())
