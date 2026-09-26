"""命令行入口：python -m transform_tree [request.json]

从文件（或省略参数时从标准输入）读取 JSON 请求，
将 JSON 响应写到标准输出。进程退出码：请求整体失败为 1，否则为 0。
"""

from __future__ import annotations

import json
import sys

from .json_api import handle_request


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) > 1:
        print("用法: python -m transform_tree [request.json]", file=sys.stderr)
        return 2
    try:
        if argv:
            with open(argv[0], "r", encoding="utf-8") as fh:
                request = json.load(fh)
        else:
            request = json.load(sys.stdin)
    except (OSError, json.JSONDecodeError) as exc:
        print(
            json.dumps(
                {"ok": False, "error": {"type": type(exc).__name__, "message": str(exc)}},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1

    response = handle_request(request)
    print(json.dumps(response, ensure_ascii=False, indent=2))
    return 0 if response.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
