"""命令行 JSON 接口（无服务进程的纯后端入口）。

用法：
    python -m robust_regression.cli < request.json > response.json
    cat request.json | python -m robust_regression.cli

从标准输入读取一个 JSON 请求，向标准输出写一个 JSON 响应。
ok=true 时退出码 0；ok=false 时退出码 1；JSON 无法解析等
调用层错误退出码 2。
"""

from __future__ import annotations

import json
import sys

from .api import fit_from_json


def main(argv: list[str] | None = None) -> int:
    raw = sys.stdin.read()
    if not raw.strip():
        json.dump(
            {
                "ok": False,
                "error": {
                    "type": "input_error",
                    "message": "标准输入为空，期望一个 JSON 请求对象",
                },
            },
            sys.stdout,
            ensure_ascii=False,
        )
        sys.stdout.write("\n")
        return 2
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        json.dump(
            {
                "ok": False,
                "error": {
                    "type": "input_error",
                    "message": f"JSON 解析失败：{exc}",
                },
            },
            sys.stdout,
            ensure_ascii=False,
        )
        sys.stdout.write("\n")
        return 2

    response = fit_from_json(payload)
    json.dump(response, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0 if response.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
