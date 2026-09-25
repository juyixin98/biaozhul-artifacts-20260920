"""命令行入口：``python -m tensor_planner.cli run request.json``。

也可从标准输入读取：``cat request.json | python -m tensor_planner.cli run -``
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Mapping

from .api import RequestError, handle_request
from .dag import DAGValidationError


def _load_request(path: str) -> Mapping[str, Any]:
    if path == "-":
        return json.load(sys.stdin)
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tensor_planner",
        description="静态张量 DAG 内存复用规划与验证",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run", help="处理一个 JSON 请求并输出报告")
    run_p.add_argument("request", help="请求 JSON 路径，或 - 表示标准输入")
    run_p.add_argument(
        "-o", "--output", help="把报告写入该文件，缺省输出到标准输出"
    )
    args = parser.parse_args(argv)

    if args.command == "run":
        try:
            spec = _load_request(args.request)
            report = handle_request(spec)
        except (RequestError, DAGValidationError) as exc:
            print(f"请求无效: {exc}", file=sys.stderr)
            return 2
        except json.JSONDecodeError as exc:
            print(f"JSON 解析失败: {exc}", file=sys.stderr)
            return 2

        text = json.dumps(report, ensure_ascii=False, indent=2)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as fh:
                fh.write(text + "\n")
        else:
            print(text)
        return 0 if report["passed"] else 1

    parser.error(f"未知命令: {args.command}")
    return 2  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main())
