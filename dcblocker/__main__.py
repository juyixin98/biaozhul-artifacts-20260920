"""命令行入口：python -m dcblocker <request.json> [...]"""

from __future__ import annotations

import argparse
import json
import sys

from .service import run_request_file


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="dcblocker",
        description="离线音频分块去偏置服务：执行 JSON 请求，输出数值指标与处理后文件",
    )
    parser.add_argument("requests", nargs="+", help="请求 JSON 文件路径（可多个）")
    args = parser.parse_args(argv)

    for path in args.requests:
        try:
            response = run_request_file(path)
        except (OSError, ValueError, KeyError) as exc:
            print(json.dumps({"request": path, "error": str(exc)}, ensure_ascii=False),
                  file=sys.stderr)
            return 1
        print(json.dumps({"request": path, **response}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
