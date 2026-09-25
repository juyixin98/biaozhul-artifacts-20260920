"""命令行入口：从文件或标准输入读取 JSON 请求，向标准输出打印 JSON 响应。

用法：
    python -m knapsack request.json
    cat request.json | python -m knapsack
"""

from __future__ import annotations

import json
import sys

from .api import solve_request


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if argv:
            with open(argv[0], "r", encoding="utf-8") as fh:
                req = json.load(fh)
        else:
            req = json.load(sys.stdin)
    except (OSError, json.JSONDecodeError) as exc:
        resp = {"status": "error", "error": {"code": "bad_json", "message": str(exc)}}
        print(json.dumps(resp, ensure_ascii=False, indent=2))
        return 2

    resp = solve_request(req)
    print(json.dumps(resp, ensure_ascii=False, indent=2))
    return 0 if resp["status"] != "error" else 1


if __name__ == "__main__":
    raise SystemExit(main())
