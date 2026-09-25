"""命令行 JSON 接口。

用法::

    python -m knapsack.cli                 # 从 stdin 读 JSON
    python -m knapsack.cli -f req.json     # 从文件读 JSON
    echo '{...}' | python -m knapsack.cli  # 管道

退出码：
    0 求解完成（status 为 optimal 或 timeout 都算正常业务结果）
    2 输入 JSON 语法错误或校验失败
"""

from __future__ import annotations

import argparse
import json
import sys

from .api import solve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m knapsack.cli",
        description="精确 0/1 背包求解器（JSON 进 / JSON 出）",
    )
    parser.add_argument("-f", "--file", help="请求 JSON 文件路径（缺省读 stdin）")
    parser.add_argument("--indent", type=int, default=2, help="输出 JSON 缩进（默认 2）")
    args = parser.parse_args(argv)

    raw = sys.stdin.read() if not args.file else open(args.file, "r", encoding="utf-8").read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        json.dump(
            {
                "ok": False,
                "error": {
                    "code": "invalid_json",
                    "message": f"请求不是合法 JSON：{exc}",
                    "field": "<body>",
                },
            },
            sys.stdout,
            ensure_ascii=False,
            indent=args.indent,
        )
        sys.stdout.write("\n")
        return 2

    response = solve(payload)
    json.dump(response, sys.stdout, ensure_ascii=False, indent=args.indent)
    sys.stdout.write("\n")
    return 0 if response.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
