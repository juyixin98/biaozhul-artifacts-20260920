#!/usr/bin/env python3
"""差速里程计 JSON 命令行入口。

用法:
    python3 main.py request.json            # 从文件读取请求
    cat request.json | python3 main.py      # 从标准输入读取
结果以 JSON 写到标准输出；输入错误时以非零码退出并把错误写到 stderr。
"""

from __future__ import annotations

import json
import sys

from diff_odom.json_io import handle_request


def main(argv: list[str]) -> int:
    if len(argv) > 2:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        raw = open(argv[1], encoding="utf-8").read() if len(argv) == 2 else sys.stdin.read()
        request = json.loads(raw)
        response = handle_request(request)
    except (OSError, json.JSONDecodeError, ValueError, KeyError, TypeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    json.dump(response, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
