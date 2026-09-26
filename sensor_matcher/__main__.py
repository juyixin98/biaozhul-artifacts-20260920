"""命令行入口：``python -m sensor_matcher request.json``。

不带参数时从标准输入读取请求。结果 JSON 写到标准输出；
请求非法时错误信息写到标准错误并以退出码 2 结束。
"""

import json
import sys

from .json_api import RequestError, process_request


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) > 1:
        print("用法: python -m sensor_matcher [request.json]", file=sys.stderr)
        return 2
    if len(argv) == 1:
        try:
            with open(argv[0], "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            print(f"无法读取请求文件: {exc}", file=sys.stderr)
            return 2
    else:
        text = sys.stdin.read()

    try:
        result = process_request(text)
    except RequestError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2

    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
