"""命令行入口：``python -m drift [--host 127.0.0.1] [--port 8000]``。"""
from __future__ import annotations

import argparse

from .service import run_server


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m drift",
        description="本地特征统计漂移监测 HTTP 服务（纯 NumPy + 标准库）",
    )
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    parser.add_argument("--port", type=int, default=8000, help="监听端口")
    args = parser.parse_args()
    run_server(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
