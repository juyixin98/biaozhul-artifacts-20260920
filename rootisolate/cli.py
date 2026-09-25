"""命令行入口：python -m rootisolate.cli [request.json]

不带参数时从标准输入读取一个 JSON 请求；带文件参数时读取该文件。
结果写标准输出，退出码：
  0 成功（包括零多项式/常数等合法状态）
  2 请求或输入非法（malformed_json / invalid_* / out_of_range）
  3 资源限制（递归/内存）
"""
from __future__ import annotations

import argparse
import sys

from .api import handle_json
from . import __version__


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="rootisolate",
        description="一维多项式实根隔离与二分细化（精确有理数后端）")
    parser.add_argument("request", nargs="?",
                        help="JSON 请求文件；省略则从标准输入读取")
    parser.add_argument("--version", action="version",
                        version=f"rootisolate {__version__}")
    args = parser.parse_args(argv)

    if args.request:
        try:
            with open(args.request, "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError as e:
            sys.stderr.write(f"无法读取请求文件：{e}\n")
            return 2
    else:
        text = sys.stdin.read()

    out, code = handle_json(text)
    sys.stdout.write(out)
    return code


if __name__ == "__main__":
    sys.exit(main())
