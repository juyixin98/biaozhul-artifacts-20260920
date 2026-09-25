"""命令行入口：python -m pcm_container run <request.json> [...]

每个参数是一个请求 JSON 文件；也可用 - 从 stdin 读取单个 JSON 对象。
结果以 JSON 打印到 stdout（多个请求时为 JSON 数组）。

退出码：
- 0 全部成功；
- 2 请求内容/PCM 容器被拒绝（PcmError，例如不支持的格式、截断、对齐错误）；
- 1 其它环境性错误（文件不存在、JSON 语法错误等）。

不启动服务、不打开界面、不播放声音。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List

from .errors import PcmError
from .service import run_request


def _load_request(path: str) -> Dict[str, Any]:
    if path == "-":
        return json.load(sys.stdin)
    with open(path, "rb") as f:
        return json.load(f)


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m pcm_container",
        description="PCM 容器校验 / 离线信号处理（纯后端，无界面）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="执行一个或多个请求 JSON 文件")
    run_p.add_argument(
        "requests",
        nargs="+",
        help="请求 JSON 文件路径（- 表示 stdin，至多出现一次）",
    )
    run_p.add_argument(
        "--indent", type=int, default=2, help="stdout JSON 缩进（默认 2）"
    )

    args = parser.parse_args(argv)

    if args.command == "run":
        multi = len(args.requests) > 1
        if "-" in args.requests and args.requests.count("-") > 1:
            print("错误：stdin(-) 至多使用一次", file=sys.stderr)
            return 1

        results: List[Any] = []
        try:
            for path in args.requests:
                try:
                    req = _load_request(path)
                    results.append(run_request(req))
                except PcmError as exc:
                    print(
                        json.dumps(
                            {
                                "ok": False,
                                "request": None if path == "-" else path,
                                "error_type": type(exc).__name__,
                                "error": str(exc),
                            },
                            ensure_ascii=False,
                            indent=args.indent,
                        ),
                        file=sys.stderr,
                    )
                    return 2
        except (OSError, json.JSONDecodeError) as exc:
            print(f"错误：无法读取/解析请求：{exc}", file=sys.stderr)
            return 1

        out = results if multi else results[0]
        json.dump(out, sys.stdout, ensure_ascii=False, indent=args.indent)
        sys.stdout.write("\n")
        return 0

    parser.error("未知命令")
    return 1  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
