"""命令行入口：python -m keyversion [--data-dir DIR] [--host 127.0.0.1] [--port 8080] serve

子命令：
    serve     启动本地 HTTP 服务（默认）
    init      初始化密钥库并执行首次轮换（得到第一个 active 版本）
    status    打印全部版本状态
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from .server import serve
from .service import KeyService
from .store import KeyStore


def _build_service(data_dir: str) -> tuple[KeyStore, KeyService]:
    store = KeyStore(data_dir).open()
    return store, KeyService(store)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="keyversion", description="密钥版本审计本地服务")
    parser.add_argument("--data-dir", default="./data", help="本地密钥库目录（默认 ./data）")
    parser.add_argument("--host", default="127.0.0.1", help="绑定地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8080, help="监听端口（默认 8080）")
    parser.add_argument("-v", "--verbose", action="store_true", help="打开调试日志")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="启动 HTTP 服务（默认命令）")
    sub.add_parser("init", help="初始化并轮换出第一个 active 版本")
    sub.add_parser("status", help="输出版本状态 JSON")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    command = args.command or "serve"
    store, service = _build_service(args.data_dir)
    try:
        if command == "init":
            if service.active_version() is None:
                info = service.rotate()
                print(json.dumps({"initialized": True, "version": info.to_dict()}, indent=2,
                                 ensure_ascii=False))
            else:
                print(json.dumps({"initialized": False, "reason": "already initialized"},
                                 indent=2, ensure_ascii=False))
        elif command == "status":
            print(json.dumps(
                {
                    "active_version": store.active_version_id(),
                    "versions": [v.to_dict() for v in service.list_versions()],
                    "audit_entries": len(service.list_audit()),
                },
                indent=2, ensure_ascii=False,
            ))
        elif command == "serve":
            print(f"数据目录: {args.data_dir}", file=sys.stderr)
            print(f"监听地址: http://{args.host}:{args.port}", file=sys.stderr)
            serve(args.host, args.port, service)
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
