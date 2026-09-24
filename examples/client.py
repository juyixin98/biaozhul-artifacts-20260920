"""命令行示例客户端：构造正常/恶意归档并调用 HTTP 接口。

用法：
    python examples/client.py health
    python examples/client.py keygen
    python examples/client.py benign        # 上传一个正常 tar
    python examples/client.py evil-paths    # 上传含绝对路径/穿越的 tar
    python examples/client.py evil-links    # 上传符号链接逃逸 tar
    python examples/client.py bomb          # 上传高压缩比 gzip 炸弹
    python examples/client.py signed <public_pem_path> <private_pem_path>
"""

from __future__ import annotations

import io
import gzip
import sys
import tarfile
import time

import httpx

BASE = "http://127.0.0.1:8000"


def _tarinfo(name: str, kind: int, *, size: int = 0, linkname: str = "") -> tarfile.TarInfo:
    m = tarfile.TarInfo(name)
    m.type = kind
    m.size = size
    m.mtime = int(time.time())
    if linkname:
        m.linkname = linkname
    return m


def build_benign() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        d = _tarinfo("docs", tarfile.DIRTYPE)
        t.addfile(d)
        payload = b"hello archive-guard"
        f = _tarinfo("docs/readme.txt", tarfile.REGTYPE, size=len(payload))
        t.addfile(f, io.BytesIO(payload))
        l = _tarinfo("docs/alias", tarfile.SYMTYPE, linkname="readme.txt")
        t.addfile(l)
    return buf.getvalue()


def build_evil_paths() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        # 绝对路径
        t.addfile(_tarinfo("/tmp/pwned-absolute", tarfile.REGTYPE, size=4),
                  io.BytesIO(b"evil"))
        # 目录穿越
        t.addfile(_tarinfo("../../pwned-traversal", tarfile.REGTYPE, size=4),
                  io.BytesIO(b"evil"))
    return buf.getvalue()


def build_evil_links() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        # 先放一个指向目标根外的符号链接，再试图经它写文件
        t.addfile(_tarinfo("escape", tarfile.SYMTYPE, linkname="../../../etc"))
        t.addfile(_tarinfo("escape/pwned", tarfile.REGTYPE, size=3),
                  io.BytesIO(b"pwn"))
    return buf.getvalue()


def build_bomb() -> bytes:
    zeros = b"\x00" * (2 * 1024 * 1024)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        t.addfile(_tarinfo("zeros.bin", tarfile.REGTYPE, size=len(zeros)),
                  io.BytesIO(zeros))
    return gzip.compress(buf.getvalue(), compresslevel=9)


def post(client: httpx.Client, path: str, data: bytes, filename: str = "a.tar",
         headers: dict | None = None) -> None:
    r = client.post(path, files={"archive": (filename, data, "application/x-tar")},
                    headers=headers or {})
    print(f"-> POST {path} [{filename}, {len(data)} bytes]")
    print(f"<- {r.status_code}")
    import json
    try:
        print(json.dumps(r.json(), ensure_ascii=False, indent=2)[:2000])
    except Exception:
        print(r.text[:1000])
    print()


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "benign"
    with httpx.Client(base_url=BASE, timeout=30) as c:
        if cmd == "health":
            r = c.get("/health")
            print(r.status_code, r.json())
        elif cmd == "keygen":
            r = c.post("/api/v1/keys/ed25519/generate")
            print(r.status_code)
            print(r.text)
        elif cmd == "benign":
            post(c, "/api/v1/archives/extract", build_benign())
        elif cmd == "precheck":
            post(c, "/api/v1/archives/precheck", build_benign())
        elif cmd == "evil-paths":
            post(c, "/api/v1/archives/precheck", build_evil_paths())
        elif cmd == "evil-links":
            post(c, "/api/v1/archives/extract", build_evil_links())
        elif cmd == "bomb":
            post(c, "/api/v1/archives/extract", build_bomb(), "a.tar.gz")
        elif cmd == "signed":
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
            priv_pem = open(sys.argv[3], "rb").read()
            pub_pem = open(sys.argv[2], "rb").read()
            data = build_benign()
            key = serialization.load_pem_private_key(priv_pem, password=None)
            assert isinstance(key, Ed25519PrivateKey)
            sig = key.sign(data).hex()
            compact_pem = b"".join(pub_pem.splitlines()).decode()
            post(c, "/api/v1/archives/extract", data,
                 headers={"X-Signature": sig, "X-Public-Key": compact_pem})
        else:
            print(__doc__)
            return 2
    return 0


if __name__ == "__main__":
        raise SystemExit(main())
