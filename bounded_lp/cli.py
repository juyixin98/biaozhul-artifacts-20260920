"""命令行入口：``python -m bounded_lp.cli request.json [-o response.json]``。

不传文件（或文件为 ``-``）时从标准输入读取请求。退出码：

0 最优/不可行/无界（得到明确结论）；1 失败（迭代上限/循环/数值崩坏）；
2 请求不合法；3 IO/用法错误。
"""

from __future__ import annotations

import argparse
import json
import sys

from bounded_lp.io_json import loads


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="blp",
        description="有界线性规划两阶段单纯形求解器（JSON 接口，纯后端）",
    )
    parser.add_argument("input", nargs="?", default="-",
                        help="请求 JSON 文件路径；省略或 '-' 表示标准输入")
    parser.add_argument("-o", "--output",
                        help="响应输出文件；省略时写到标准输出")
    parser.add_argument("--indent", type=int, default=2,
                        help="JSON 缩进空格数（默认 2，0 表示紧凑输出）")
    args = parser.parse_args(argv)

    try:
        if args.input == "-":
            text = sys.stdin.read()
        else:
            with open(args.input, "r", encoding="utf-8") as fh:
                text = fh.read()
    except OSError as exc:
        print(json.dumps({"status": "invalid_request",
                          "errors": [f"无法读取输入：{exc}"]}, ensure_ascii=False),
              file=sys.stderr)
        return 3

    response = loads(text)
    indent = args.indent if args.indent > 0 else None
    rendered = json.dumps(response, indent=indent, ensure_ascii=False, allow_nan=False)

    try:
        if args.output:
            with open(args.output, "w", encoding="utf-8") as fh:
                fh.write(rendered + "\n")
        else:
            print(rendered)
    except OSError as exc:
        print(f"无法写出结果：{exc}", file=sys.stderr)
        return 3

    return _exit_code(response["status"])


def _exit_code(status: str) -> int:
    if status in ("optimal", "infeasible", "unbounded"):
        return 0
    if status == "failed":
        return 1
    return 2  # invalid_request


if __name__ == "__main__":
    sys.exit(main())
