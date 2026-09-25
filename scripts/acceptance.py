#!/usr/bin/env python3
"""端到端验收：启动编译好的 multipart-server，用原始 TCP socket 发送各类请求，
逐条核对 HTTP 状态码与错误标签。全部通过则退出码 0。

用法：
    python3 scripts/acceptance.py            # 自动 cargo build --release 后运行
    python3 scripts/acceptance.py --no-build

覆盖场景：
  1. 正常多 part（含空 part + 含边界前缀的二进制 part）→ 200，SHA-256 一致
  2. 与 #1 相同的请求，但整包逐字节发送（服务端读取块也调为 1）→ 200，响应逐字节一致
  3. 缺结束边界 → 400 truncated
  4. part 数超限 → 413 too_many_parts
  5. 单 part 超限 → 413 part_too_large
  6. 总大小超限 → 413 total_too_large
  7. 头超限 → 413 header_too_large
  8. 缺 Content-Disposition → 400 missing_disposition
  9. Content-Type 不是 multipart/form-data → 400 invalid_boundary
"""

import hashlib
import http.client
import os
import socket
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "target", "release", "multipart-server")
BOUNDARY = "B"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start_server(port, read_size, limits):
    env = dict(os.environ)
    env["MULTIPART_READ_SIZE"] = str(read_size)
    env.update(
        MULTIPART_MAX_PARTS=str(limits["parts"]),
        MULTIPART_MAX_HEADERS=str(limits["headers"]),
        MULTIPART_MAX_PART=str(limits["part"]),
        MULTIPART_MAX_TOTAL=str(limits["total"]),
    )
    log = open(os.path.join(ROOT, f"target/server-{port}.log"), "wb")
    proc = subprocess.Popen([BIN, str(port)], env=env, stdout=log, stderr=log)
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", port), 0.2):
                return proc
        except OSError:
            time.sleep(0.05)
    proc.kill()
    raise RuntimeError(f"server on {port} did not start")


def raw_send(port, body, content_type="multipart/form-data; boundary=" + BOUNDARY,
             slow=False, method="POST", extra_headers=None, declare_length=None):
    """用裸 socket 发请求；slow=True 时逐字节写入。返回 (status, body_bytes)。"""
    length = len(body) if declare_length is None else declare_length
    head = (
        f"{method} /upload HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
        f"Content-Type: {content_type}\r\nContent-Length: {length}\r\n"
    )
    if extra_headers:
        head += extra_headers
    head += "\r\n"
    payload = head.encode() + body
    s = socket.create_connection(("127.0.0.1", port), timeout=10)
    try:
        if slow:
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            for ch in payload:
                s.sendall(bytes([ch]))
        else:
            s.sendall(payload)
    except (BrokenPipeError, ConnectionResetError):
        # 超限请求可能在我们还没发完时就被服务端拒绝并关闭；继续读取已到达的响应。
        pass
    chunks = []
    while True:
        try:
            buf = s.recv(4096)
        except socket.timeout:
            break
        except ConnectionResetError:
            # 服务端拒绝超限请求后立即关闭，而客户端还有未读完的请求体，
            # 内核可能回 RST；响应通常已经在前面的 recv 中到达。
            break
        if not buf:
            break
        chunks.append(buf)
    s.close()
    raw = b"".join(chunks)
    status_line = raw.split(b"\r\n", 1)[0].decode("latin1")
    status = int(status_line.split()[1])
    resp_body = raw.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in raw else b""
    return status, resp_body


def part(name, data, filename=None):
    d = f"--{BOUNDARY}\r\n".encode()
    if filename is None:
        d += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
    else:
        d += (f'Content-Disposition: form-data; name="{name}"; '
              f'filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n').encode()
    return d + data + b"\r\n"


RESULTS = []


def check(title, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    RESULTS.append((title, cond))
    print(f"[{tag}] {title}" + (f"  {detail}" if detail and not cond else ""))


def main():
    if "--no-build" not in sys.argv:
        print("$ cargo build --release")
        r = subprocess.run(["cargo", "build", "--release"], cwd=ROOT)
        if r.returncode != 0:
            return 1

    # 先生成二进制载荷
    subprocess.run([sys.executable, os.path.join(ROOT, "examples", "gen_binary_payload.py")],
                   cwd=ROOT, check=True)
    with open(os.path.join(ROOT, "examples", "binary-payload.bin"), "rb") as f:
        binary = f.read()
    binary_sha = hashlib.sha256(binary).hexdigest()

    default_port = free_port()
    tiny_port = free_port()
    total_port = free_port()
    p1 = start_server(default_port, 1, {"parts": 32, "headers": 16384,
                                        "part": 8 * 1024 * 1024, "total": 64 * 1024 * 1024})
    # tiny 服务：所有上限都极小，用于恶意超限验证
    p2 = start_server(tiny_port, 1, {"parts": 2, "headers": 128, "part": 10, "total": 256})
    # 只把“总大小”上限调小、其他放宽，确保能单独触发 total_too_large
    p3 = start_server(total_port, 1, {"parts": 32, "headers": 16384,
                                      "part": 10_000_000, "total": 200})

    try:
        # 1) 正常：普通字段 + 空 part + 二进制 part
        body = (part("field1", b"hello world")
                + part("empty_field", b"")
                + part("file", binary, "f.bin")
                + f"--{BOUNDARY}--\r\n".encode())
        st, resp = raw_send(default_port, body)
        text = resp.decode()
        check("1a 正常多 part 返回 200", st == 200, f"status={st} body={text}")
        check("1b part_count=3", b'"part_count":3' in resp, text)
        check("1c 空 part size=0", b'"name":"empty_field","filename":null,"size":0' in resp, text)
        check("1d 二进制 part 大小与 SHA-256 一致",
              f'"size":{len(binary)}'.encode() in resp and binary_sha.encode() in resp, text)
        check("1e 正文里的近似边界没有误判（ok:true）", b'"ok":true' in resp, text)

        # 2) 逐字节发送同一请求
        st2, resp2 = raw_send(default_port, body, slow=True)
        check("2a 逐字节发送返回 200", st2 == 200, f"status={st2}")
        check("2b 逐字节发送响应与普通发送完全一致", resp2 == resp,
              f"{resp2!r} != {resp!r}")

        # 3) 缺结束边界：只发到一个 part 的正文后 CRLF
        truncated = part("a", b"v")  # 结尾是 CRLF，没有 --B--
        st, resp = raw_send(default_port, truncated)
        check("3 缺结束边界 → 400 truncated",
              st == 400 and b'"truncated"' in resp, f"status={st} body={resp!r}")

        # 4) part 数超限（tiny: 2）
        over_parts = part("a", b"v") + part("b", b"w") + part("c", b"x") + b"--B--\r\n"
        st, resp = raw_send(tiny_port, over_parts)
        check("4 超过 part 数上限 → 413 too_many_parts",
              st == 413 and b"too_many_parts" in resp, f"status={st} body={resp!r}")

        # 5) 单 part 超限（tiny: 10 字节）—— 构造足够小的头+11 字节正文，且总量 < 256
        over_part = part("a", b"X" * 11) + b"--B--\r\n"
        st, resp = raw_send(tiny_port, over_part)
        check("5 单 part 超上限 → 413 part_too_large",
              st == 413 and b"part_too_large" in resp, f"status={st} body={resp!r}")

        # 6) 总大小超限（专用服务：total=200，part/头/数量都放宽）
        over_total = part("a", b"Z" * 300) + b"--B--\r\n"
        st, resp = raw_send(total_port, over_total)
        check("6 总大小超上限 → 413 total_too_large",
              st == 413 and b"total_too_large" in resp, f"status={st} body={resp!r}")

        # 7) 头超限（tiny: 128 字节）—— 单个 disposition 头撑大
        big = b"--B\r\nContent-Disposition: form-data; name=\"a\"; "
        big += b"x=\"" + b"q" * 300 + b"\"\r\n\r\nv\r\n--B--\r\n"
        st, resp = raw_send(tiny_port, big)
        check("7 头部超上限 → 413 header_too_large",
              st == 413 and b"header_too_large" in resp, f"status={st} body={resp!r}")

        # 8) 缺 Content-Disposition
        no_disp = b"--B\r\nContent-Type: text/plain\r\n\r\nv\r\n--B--\r\n"
        st, resp = raw_send(default_port, no_disp)
        check("8 缺 Content-Disposition → 400 missing_disposition",
              st == 400 and b"missing_disposition" in resp, f"status={st} body={resp!r}")

        # 9) 错误 Content-Type
        st, resp = raw_send(default_port, b"a=b",
                            content_type="application/x-www-form-urlencoded")
        check("9 非 multipart Content-Type → 400 invalid_boundary",
              st == 400 and b"invalid_boundary" in resp, f"status={st} body={resp!r}")

        # 10) 二进制载荷本身的自校验：近似边界数量充足（确保测试真的覆盖到）
        needles = [b"--B", b"--B-", b"--B--", b"\r\n--B-x", b"\r\n--B\r"]
        found = {n: n in binary for n in needles}
        check("10 二进制载荷确实包含全部近似边界模式",
              all(found.values()), f"missing={[n for n, ok in found.items() if not ok]}")
    finally:
        for p in (p1, p2, p3):
            p.terminate()
            p.wait(timeout=5)

    failed = [t for t, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    if failed:
        print("FAILED:", *failed, sep="\n  - ")
        return 1
    print("ALL ACCEPTANCE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
