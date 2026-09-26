"""命令行入口：python -m collision_detection.cli request.json [-o out.json]

不提供服务端口与前端；仅做离线文件计算。
"""

from __future__ import annotations

import argparse
import json
import sys

from .io_json import build_error_response, build_response, load_request


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="运动碰撞连续检测（CCD）离线 JSON 计算器"
    )
    parser.add_argument("request", help="请求 JSON 文件路径")
    parser.add_argument(
        "-o", "--output", help="响应输出路径（缺省输出到标准输出）"
    )
    args = parser.parse_args(argv)

    try:
        request = load_request(args.request)
        response = build_response(request)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        response = build_error_response(f"请求处理失败：{exc}")
        text = json.dumps(response, ensure_ascii=False, indent=2)
        if args.output:
            with open(args.output, "w", encoding="utf-8") as fh:
                fh.write(text + "\n")
        else:
            print(text)
        return 1

    text = json.dumps(response, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
