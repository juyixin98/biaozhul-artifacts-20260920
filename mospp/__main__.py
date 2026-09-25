"""命令行接口：``python -m mospp request.json`` 或从标准输入读取。

退出码：
* 0 —— 请求处理成功（status 为 ok / no_path / truncated 均算成功）；
* 2 —— 请求校验失败（RequestError / LimitExceeded）；
* 1 —— 内部错误。
"""

from __future__ import annotations

import argparse
import json
import sys

from .api import run_request
from .errors import LimitExceeded, RequestError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m mospp",
        description="双目标（时间、费用）最短路 JSON 接口",
    )
    parser.add_argument(
        "request_file",
        nargs="?",
        help="JSON 请求文件路径；省略时从标准输入读取",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="输出紧凑 JSON（默认带缩进、保留中文）",
    )
    args = parser.parse_args(argv)

    try:
        if args.request_file:
            with open(args.request_file, "r", encoding="utf-8") as f:
                raw = f.read()
        else:
            raw = sys.stdin.read()
    except OSError as exc:
        print(
            json.dumps(
                {"error": "io_error", "message": f"无法读取请求：{exc}"},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1

    try:
        req = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(
            json.dumps(
                {
                    "error": "invalid_json",
                    "message": f"JSON 解析失败（第 {exc.lineno} 行第 {exc.colno} 列）：{exc.msg}",
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2

    try:
        resp = run_request(req)
    except RequestError as exc:
        print(
            json.dumps(exc.to_dict(), ensure_ascii=False, indent=2),
            file=sys.stderr,
        )
        return 2
    except LimitExceeded as exc:
        payload = {
            "error": exc.code,
            "message": exc.message,
        }
        if exc.field:
            payload["field"] = exc.field
        print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2

    indent = None if args.compact else 2
    print(json.dumps(resp, ensure_ascii=False, indent=indent))
    return 0


if __name__ == "__main__":
    sys.exit(main())
