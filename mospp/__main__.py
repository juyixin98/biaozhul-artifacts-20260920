"""命令行入口：``python -m mospp [request.json]``。

* 带文件参数：从文件读取 JSON 请求；
* 不带参数：从标准输入读取；
* 求解结果以缩进 JSON 写到标准输出；
* 退出码：ok/truncated/unreachable 均为 0（unreachable 不是错误，是一种结果）；
  invalid_request/limit_exceeded/internal_error 为 1。
"""

from __future__ import annotations

import json
import sys

from .api import solve_request
from .errors import status

SUCCESS_STATUSES = {status.OK, status.TRUNCATED, status.UNREACHABLE}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) > 1:
        print("用法: python -m mospp [request.json] （省略文件名则从 stdin 读取）",
              file=sys.stderr)
        return 2
    try:
        if len(argv) == 1:
            with open(argv[0], "r", encoding="utf-8") as f:
                req = json.load(f)
        else:
            req = json.load(sys.stdin)
    except (OSError, json.JSONDecodeError) as e:
        print(
            json.dumps(
                {"status": status.INVALID_REQUEST, "error": f"无法读取/解析 JSON: {e}"},
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 1

    resp = solve_request(req)
    print(json.dumps(resp, ensure_ascii=False, indent=2))
    return 0 if resp["status"] in SUCCESS_STATUSES else 1


if __name__ == "__main__":
    sys.exit(main())
