"""命令行 JSON 接口（stdin 或文件，无网络服务）。

用法::

    python -m adaptive_integration.cli < examples/request_smooth.json
    echo '{...}' | python -m adaptive_integration.cli
    python -m adaptive_integration.cli req1.json req2.json   # 批量，逐行输出

退出码：
    0  正常处理（包括 failed 状态——不收敛是正常的业务结果）；
    2  输入本身无法读取/解析（文件错误、非法 JSON、顶层不是对象）。
"""

from __future__ import annotations

import json
import sys

from .api import handle_request


def _process_text(text: str) -> int:
    try:
        request = json.loads(text)
    except json.JSONDecodeError as exc:
        resp = {"status": "invalid_request", "converged": False,
                "value": None, "error_estimate": None,
                "error_code": "MALFORMED_JSON",
                "message": f"输入不是合法 JSON: {exc.msg} "
                           f"(line {exc.lineno}, col {exc.colno})"}
        print(json.dumps(resp, ensure_ascii=False))
        return 2
    print(json.dumps(handle_request(request), ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        return _process_text(sys.stdin.read())
    code = 0
    for path in argv:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            resp = {"status": "invalid_request", "converged": False,
                    "value": None, "error_estimate": None,
                    "error_code": "IO_ERROR",
                    "message": f"无法读取请求文件 {path!r}: {exc}"}
            print(json.dumps(resp, ensure_ascii=False))
            code = 2
            continue
        if _process_text(text) == 2:
            code = 2
    return code


if __name__ == "__main__":
    raise SystemExit(main())
