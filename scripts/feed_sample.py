#!/usr/bin/env python3
"""把一个合成报文文件发给 tls-observer-server。

用法: feed_sample.py HOST PORT FILE [每片字节数] [片间延迟秒]

- 默认一次性发送整个文件后半关闭写端（服务端读到 EOF）。
- 指定“每片字节数”后按固定大小分片发送，用来验证服务端的增量重组。
"""
import socket
import sys
import time


def main() -> int:
    if len(sys.argv) < 4:
        print(__doc__, file=sys.stderr)
        return 2
    host, port, path = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    chunk = int(sys.argv[4]) if len(sys.argv) > 4 else 0
    delay = float(sys.argv[5]) if len(sys.argv) > 5 else 0.0

    with open(path, "rb") as f:
        data = f.read()

    with socket.create_connection((host, port), timeout=10) as s:
        if chunk and chunk > 0:
            for i in range(0, len(data), chunk):
                s.sendall(data[i:i + chunk])
                time.sleep(delay)
        else:
            s.sendall(data)
        s.shutdown(socket.SHUT_WR)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
