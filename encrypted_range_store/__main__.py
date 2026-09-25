"""命令行入口：python -m encrypted_range_store serve --data-dir ./data

子命令：
    serve [--host 127.0.0.1] [--port 8080] [--block-size 65536]
          [--data-dir DIR]        启动本地 HTTP 服务
    put  FILE OBJECT_ID           本地直接写入对象（不走 HTTP）
    get  OBJECT_ID [--range S-E]  本地读取对象或范围 [start, end)
"""

from __future__ import annotations

import argparse
import os
import sys

from .format import DEFAULT_BLOCK_SIZE
from .server import build_server
from .service import EncryptedObjectStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="encrypted_range_store")
    parser.add_argument("--data-dir", default=os.environ.get("ERS_DATA_DIR", "./data"))
    parser.add_argument("--block-size", type=int, default=DEFAULT_BLOCK_SIZE)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_serve = sub.add_parser("serve", help="启动本地 HTTP 服务")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8080)

    p_put = sub.add_parser("put", help="本地直接写入对象")
    p_put.add_argument("file")
    p_put.add_argument("object_id")

    p_get = sub.add_parser("get", help="本地读取对象")
    p_get.add_argument("object_id")
    p_get.add_argument("--range", dest="rng", default=None,
                       help="半开区间，形如 100-200，即 [100,200)")

    args = parser.parse_args(argv)
    store = EncryptedObjectStore(args.data_dir, block_size=args.block_size)

    if args.cmd == "serve":
        token = os.environ.get("ERS_TOKEN")
        httpd = build_server(args.host, args.port, args.data_dir,
                             args.block_size, token)
        auth = "，Bearer 认证已启用" if token else ""
        print(f"监听 http://{args.host}:{args.port}（数据目录 {args.data_dir}{auth}）")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n停止")
        return 0

    if args.cmd == "put":
        with open(args.file, "rb") as f:
            data = f.read()
        store.put(args.object_id, data)
        print(f"已写入对象 {args.object_id}，明文 {len(data)} 字节")
        return 0

    if args.cmd == "get":
        if args.rng:
            s, e = args.rng.split("-", 1)
            data = store.get_range(args.object_id, int(s), int(e))
        else:
            data = store.get(args.object_id)
        sys.stdout.buffer.write(data)
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
