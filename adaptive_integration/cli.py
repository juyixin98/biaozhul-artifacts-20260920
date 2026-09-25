"""命令行 JSON 接口。

从文件或 stdin 读取单个积分请求，把结果 JSON 写到 stdout。

退出码语义：
    0  收敛
    1  积分失败（不收敛/奇点/预算耗尽，响应体仍为合法 JSON）
    2  请求无法处理（非法 JSON、参数越界、表达式无法编译）
"""

from __future__ import annotations

import argparse
import json
import sys

from .api import integrate_request
from .io_layer import result_to_json


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m adaptive_integration.cli",
        description="自适应积分 JSON 后端（Gauss-Kronrod 7-15 / 自适应 Simpson）",
    )
    parser.add_argument(
        "-f", "--file", help="请求 JSON 文件路径；缺省时从 stdin 读取"
    )
    args = parser.parse_args(argv)

    if args.file:
        try:
            with open(args.file, "r", encoding="utf-8") as fh:
                raw = fh.read()
        except OSError as ex:
            payload = {
                "status": "failed",
                "error_code": "INVALID_REQUEST",
                "error_message": f"无法读取请求文件：{ex}",
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 2
    else:
        raw = sys.stdin.read()

    try:
        request = json.loads(raw)
    except json.JSONDecodeError as ex:
        payload = {
            "status": "failed",
            "error_code": "INVALID_REQUEST",
            "error_message": f"JSON 语法错误：{ex}",
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2

    result = integrate_request(request)

    # 参数越界与表达式编译错误属于客户端错误 -> 退出码 2
    client_error = result.error_code in ("INVALID_REQUEST", "PARSE_ERROR")
    print(result_to_json(result))
    if result.converged:
        return 0
    return 2 if client_error else 1


if __name__ == "__main__":
    raise SystemExit(main())
