#!/usr/bin/env python3
"""reasm-server 的零依赖测试客户端（只用标准库 socket）。

读取 examples/requests.ndjson，逐行发给本地 TCP 服务，打印每行响应；
对重组完成的 300 字节数据报，自动对照 examples/original_payload.hex，
验证重组结果与完整原始载荷逐字节一致。

用法：
    python3 examples/client.py [--port 9000]
"""

import argparse
import json
import socket
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9000)
    args = ap.parse_args()

    here = Path(__file__).resolve().parent
    requests = (here / "requests.ndjson").read_text().splitlines()
    expected_hex = (here / "original_payload.hex").read_text().strip()

    with socket.create_connection((args.host, args.port), timeout=5) as sock:
        f = sock.makefile("rwb", buffering=0)
        failures = 0
        for line in requests:
            line = line.strip()
            if not line:
                continue
            f.write(line.encode() + b"\n")
            resp = json.loads(f.readline().decode())
            print(json.dumps(resp, separators=(",", ":")))

            if resp.get("result") == "completed" and resp.get("total_length") == 300:
                got = resp["payload_hex"]
                ok = got == expected_hex
                print(f"  -> payload vs original: {'MATCH' if ok else 'MISMATCH'}")
                failures += 0 if ok else 1
            if resp.get("status") == "error":
                print(f"  -> server reported error: {resp.get('error')}")
        print(f"done, payload mismatches: {failures}")
        return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
