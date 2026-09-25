"""命令行入口：从 JSON 文件（或标准输入）读取请求，把响应 JSON 写到标准输出。

用法：
    python -m kalman_missing request.json > response.json
    cat request.json | python -m kalman_missing

退出码：0 = 滤波成功；2 = 请求非法或数值失败（响应中携带 error 字段）。
"""

import json
import sys

from .json_interface import run_request


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) > 1:
        print("用法: python -m kalman_missing [request.json]", file=sys.stderr)
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
                {"status": "error",
                 "error": {"code": "invalid_input",
                           "message": f"无法读取请求: {exc}"}},
                ensure_ascii=False,
            )
        )
        return 2

    response = run_request(request)
    json.dump(response, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0 if response["status"] == "ok" else 2


if __name__ == "__main__":
    sys.exit(main())
