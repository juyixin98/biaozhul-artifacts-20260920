"""命令行接口：``python -m packing.cli [request.json]``。

* 从给定文件读取请求 JSON；不给文件名则从标准输入读取。
* 成功（status=ok）时响应 JSON 写到标准输出，退出码 0。
* status=error（输入不合法）时错误响应写到标准输出，退出码 1。
* status=failed（内部自检失败）退出码 2。
* 请求本身不是合法 JSON 时退出码 3。

示例::

    python -m packing.cli examples/request_example.json
    cat req.json | python -m packing.cli
"""

from __future__ import annotations

import json
import sys

from .api import solve


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    try:
        if argv:
            with open(argv[0], "r", encoding="utf-8") as f:
                req = json.load(f)
        else:
            req = json.load(sys.stdin)
    except FileNotFoundError:
        print(json.dumps({"status": "error",
                          "error": {"code": "file_not_found",
                                    "message": "请求文件不存在: %s" % argv[0]}},
                         ensure_ascii=False, indent=2))
        return 3
    except json.JSONDecodeError as e:
        print(json.dumps({"status": "error",
                          "error": {"code": "invalid_json",
                                    "message": "请求不是合法 JSON: %s" % e}},
                         ensure_ascii=False, indent=2))
        return 3

    resp, _http_status = solve(req)
    print(json.dumps(resp, ensure_ascii=False, indent=2))
    return {"ok": 0, "error": 1, "failed": 2}.get(resp["status"], 2)


if __name__ == "__main__":
    raise SystemExit(main())
