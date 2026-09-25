"""命令行入口：从文件或标准输入读取 JSON 请求，向标准输出写 JSON 响应。

用法::

    python -m sparse_cg.cli [request.json]            # 文件或 stdin
    echo '{...}' | python -m sparse_cg.cli
    cat request.json | python -m sparse_cg.cli -      # '-' 显式表示 stdin

退出码：
    0  请求合法（无论求解收敛与否——失败状态在 JSON 中给出）
    1  输入非法（文件无法读取、JSON 语法错误）
    2  用法错误
"""

from __future__ import annotations

import json
import sys

from .io_api import solve_request


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) > 1:
        print("用法: python -m sparse_cg.cli [request.json | -]", file=sys.stderr)
        return 2

    source = argv[0] if argv else "-"
    try:
        if source == "-":
            raw = sys.stdin.read()
        else:
            try:
                with open(source, "r", encoding="utf-8") as fh:
                    raw = fh.read()
            except OSError as exc:
                _emit_json_error("file_unreadable", f"无法读取请求文件: {exc}")
                return 1
    except KeyboardInterrupt:
        return 130

    if not raw.strip():
        _emit_json_error("empty_request", "请求为空：需要一个 JSON 对象")
        return 1

    try:
        payload = _loads_strict_json(raw)
    except _StrictJsonError as exc:
        _emit_json_error("invalid_json", str(exc))
        return 1
    except json.JSONDecodeError as exc:
        _emit_json_error(
            "invalid_json",
            f"JSON 语法错误（第 {exc.lineno} 行第 {exc.colno} 列）: {exc.msg}",
        )
        return 1

    response = solve_request(payload)
    json.dump(response, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


class _StrictJsonError(ValueError):
    """JSON 语法合法但含 NaN/Infinity 等非标准常量。"""


def _loads_strict_json(raw: str):
    """json 的严格解析：拒绝 NaN / Infinity / -Infinity。

    Python 的 json 模块默认把这些非标准 JS 常量解析成 float('nan') 等；
    标准 JSON（RFC 8259）只有 null/true/false 三个字面量。
    parse_constant 回调无法对嵌套位置可靠报错，因此这里走
    “正常解析 → 递归拒绝非有限浮点”的方式（字符串里的 "NaN" 文本不受影响）。
    """
    import math

    decoder = json.JSONDecoder()
    obj, end = decoder.raw_decode(raw)
    if raw[end:].strip():
        raise json.JSONDecodeError(
            "JSON 文本结束后还有多余内容", raw, end
        )

    def walk(v) -> None:
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            raise _StrictJsonError(
                "JSON 不允许 NaN/Infinity/-Infinity 常量"
                "（标准 JSON 数值必须有限；如确需缺失值请用 null）"
            )
        if isinstance(v, dict):
            for item in v.values():
                walk(item)
        elif isinstance(v, list):
            for item in v:
                walk(item)

    walk(obj)
    return obj


def _emit_json_error(code: str, message: str) -> None:
    json.dump(
        {"ok": False, "error": {"code": code, "message": message}},
        sys.stdout,
        ensure_ascii=False,
        indent=2,
    )
    sys.stdout.write("\n")


if __name__ == "__main__":
    raise SystemExit(main())
