"""命令行 JSON 接口。

用法::

    python -m rootisolation examples/request_basic.json
    cat request.json | python -m rootisolation
    python -m rootisolation --pretty request.json > response.json

退出码：0 成功（status=ok）；1 内部错误；2 请求非法；3 算法达到限制
（status 为 isolation_limit / refinement_limit，此时仍输出完整 JSON）。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .api import InvalidRequest, solve_poly


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="rootisolation",
        description="一维多项式实根隔离与二分细化（精确有理数）JSON 接口",
    )
    parser.add_argument("request", nargs="?",
                        help="请求 JSON 文件路径；省略则从 stdin 读取")
    parser.add_argument("--pretty", action="store_true",
                        help="缩进输出 JSON")
    args = parser.parse_args(argv)

    raw = open(args.request, encoding="utf-8").read() if args.request \
        else sys.stdin.read()
    try:
        req: Any = json.loads(raw)
    except json.JSONDecodeError as e:
        payload = {
            "status": "invalid_request",
            "errors": [{"code": "bad_json", "message": f"JSON 解析失败: {e}"}],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2

    try:
        result = solve_poly(req)
    except InvalidRequest as e:
        payload = {
            "status": "invalid_request",
            "errors": [{"code": e.code, "message": e.message}],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 2
    except Exception as e:  # noqa: BLE001 - CLI 边界必须如实报告任何异常
        payload = {
            "status": "internal_error",
            "errors": [{"code": "internal_error",
                        "message": f"{type(e).__name__}: {e}"}],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1

    indent = 2 if args.pretty else None
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=indent))
    return 3 if result.status != "ok" else 0


if __name__ == "__main__":
    sys.exit(main())
