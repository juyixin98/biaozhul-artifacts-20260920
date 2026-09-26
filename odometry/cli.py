"""JSON 命令行入口。

用法：
    python -m odometry.cli input.json              # 结果写 stdout
    python -m odometry.cli input.json -o out.json  # 结果写文件
    cat input.json | python -m odometry.cli        # 从 stdin 读

退出码：0 成功；2 输入非法（错误信息以 JSON 输出到 stderr）。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional, Sequence

from .pipeline import run_odometry


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="odometry-cli",
        description="差速轮编码器里程计离线计算（JSON 输入 / JSON 输出）",
    )
    parser.add_argument(
        "input",
        nargs="?",
        help="输入 JSON 文件路径；省略时从 stdin 读取",
    )
    parser.add_argument("-o", "--output", help="输出 JSON 文件路径；省略时写 stdout")
    parser.add_argument(
        "--pretty", action="store_true", help="以缩进格式输出 JSON"
    )
    args = parser.parse_args(argv)

    try:
        raw_text = _read_input(args.input)
        request = json.loads(raw_text)
    except (OSError, json.JSONDecodeError) as exc:
        return _fail(f"无法读取输入 JSON: {exc}")

    try:
        result = run_odometry(request)
    except ValueError as exc:
        return _fail(f"输入校验失败: {exc}")

    indent = 2 if args.pretty else None
    output_text = json.dumps(result, ensure_ascii=False, indent=indent)
    if args.output:
        try:
            with open(args.output, "w", encoding="utf-8") as fh:
                fh.write(output_text + "\n")
        except OSError as exc:
            return _fail(f"无法写入输出文件: {exc}")
    else:
        print(output_text)
    return 0


def _read_input(path: Optional[str]) -> str:
    if path is None:
        return sys.stdin.read()
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _fail(message: str) -> int:
    print(json.dumps({"error": message}, ensure_ascii=False), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
