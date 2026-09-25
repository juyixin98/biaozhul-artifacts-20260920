#!/usr/bin/env python3
"""encrypted_range_store 请求样例（纯标准库 http.client）。

用法：python examples/requests.py
脚本自行选择空闲端口、启动本地服务，演示上传、首尾/跨块范围读取、
空对象、篡改与密文交换。
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Client:
    def __init__(self, port: int):
        self.port = port

    def request(self, method: str, path: str, body: bytes | None = None,
                headers: dict | None = None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        data = r.read()
        hdrs = dict(r.getheaders())
        c.close()
        return r.status, hdrs, data


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    port = free_port()
    data_dir = tempfile.mkdtemp(prefix="ers-example-")
    # 64 字节小块，便于演示满块 + 最后短块。
    proc = subprocess.Popen(
        [sys.executable, "-m", "encrypted_range_store",
         "--data-dir", data_dir, "--block-size", "64",
         "serve", "--port", str(port)],
        cwd=root, stdout=subprocess.DEVNULL,
    )
    time.sleep(1.0)
    api = Client(port)

    try:
        print("healthz:", api.request("GET", "/healthz")[0])

        plain = bytes((i * 7) % 256 for i in range(200))
        st, _, body = api.request("PUT", "/objects/demo", plain,
                                  {"Content-Length": str(len(plain))})
        print("PUT demo:", st, json.loads(body))
        st, _, _ = api.request("PUT", "/objects/empty", b"",
                               {"Content-Length": "0"})
        print("PUT empty:", st)

        st, _, got = api.request("GET", "/objects/demo")
        assert st == 200 and got == plain
        print("整对象一致:", len(got), "字节")

        for label, rng, lo, hi in [
            ("首字节", "bytes=0-0", 0, 1),
            ("首块", "bytes=0-63", 0, 64),
            ("尾字节（短块）", "bytes=199-199", 199, 200),
            ("跨块", "bytes=60-140", 60, 141),
            ("后缀16", "bytes=-16", 184, 200),
        ]:
            st, hdrs, got = api.request("GET", "/objects/demo",
                                        headers={"Range": rng})
            ok = st == 206 and got == plain[lo:hi]
            print(f"{label}: {st} {hdrs.get('Content-Range')} 校验={ok}")
            assert ok

        st, hdrs, got = api.request("GET", "/objects/empty")
        print("空对象 GET:", st, "长度", len(got))
        st, _, _ = api.request("GET", "/objects/empty",
                               headers={"Range": "bytes=0-0"})
        print("空对象 Range:", st, "(期望 416)")

        # 篡改：直接翻转落盘密文一个字节。
        path = os.path.join(data_dir, "objects", "demo")
        blob = bytearray(Path(path).read_bytes())
        blob[-30] ^= 0xFF
        Path(path).write_bytes(blob)
        st, _, body = api.request("GET", "/objects/demo")
        leak = any(plain[i:i + 8] in body for i in range(0, 193))
        print("篡改后:", st, json.loads(body)["error"], "| 明文泄漏:", leak)
        assert st == 409 and not leak

        # 恢复 + 密文交换。
        api.request("PUT", "/objects/demo", plain,
                    {"Content-Length": str(len(plain))})
        api.request("PUT", "/objects/A", b"AAAA-secret-A" * 20,
                    {"Content-Length": str(len(b"AAAA-secret-A" * 20))})
        api.request("PUT", "/objects/B", b"BBBB-other-B-" * 20,
                    {"Content-Length": str(len(b"BBBB-other-B-" * 20))})
        Path(os.path.join(data_dir, "objects", "A")).write_bytes(
            Path(os.path.join(data_dir, "objects", "B")).read_bytes())
        st, _, body = api.request("GET", "/objects/A")
        print("密文交换后读 A:", st, json.loads(body)["error"])
        assert st == 409 and b"BBBB" not in body
        print("密文交换后读 B:", api.request("GET", "/objects/B")[0], "(期望 200)")

        print("\n全部样例断言通过。")
        return 0
    finally:
        proc.terminate()
        proc.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
