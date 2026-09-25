#!/usr/bin/env python3
"""签名客户端 + 端到端攻击场景演示。

子命令：
    call   发送单个签名请求（支持重放同一签名、篡改正文等选项）
    demo   起好服务后一键演示全部场景

请求签名由 anti_replay.signing.sign_request 生成，与服务端同一套规范化规则。
仅依赖标准库 http.client 发送 HTTP。
"""

from __future__ import annotations

import argparse
import json
import os
import secrets as pysecrets
import sys
import threading
import time
from urllib.parse import urlsplit

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from anti_replay.canonical import (  # noqa: E402
    CanonicalizationError,
    canonical_request_target,
)
from anti_replay.keys import load_keystore  # noqa: E402
from anti_replay.signing import sign_request  # noqa: E402


def _new_nonce() -> str:
    return pysecrets.token_urlsafe(18)


def _host_port(base_url: str) -> tuple[bool, str, int]:
    parts = urlsplit(base_url)
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"bad base URL: {base_url!r}")
    host = parts.hostname or "127.0.0.1"
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return parts.scheme == "https", host, port


def build_signed_headers(
    *, key: bytes, key_id: str, method: str, target: str,
    body: bytes, timestamp: int | None = None, nonce: str | None = None,
) -> tuple[dict[str, str], str]:
    """生成四个认证头；同时返回规范请求串（便于演示展示）。"""
    ts = str(int(time.time()) if timestamp is None else timestamp)
    n = nonce or _new_nonce()
    signature, canonical_request, _, _ = sign_request(
        key=key, key_id=key_id, method=method, target=target,
        body=body, timestamp=ts, nonce=n,
    )
    return {
        "Content-Type": "application/json",
        "X-Auth-Key-Id": key_id,
        "X-Auth-Timestamp": ts,
        "X-Auth-Nonce": n,
        "X-Auth-Signature": signature,
    }, canonical_request


def send(
    base_url: str, method: str, target: str,
    headers: dict[str, str], body: bytes,
) -> tuple[int, dict, bytes]:
    """用裸 TCP 发送请求，逐字节保留 request-target。

    不使用 ``http.client.request``：它会在客户端侧把 ``/./api`` 这类点段
    规范化掉，导致无法演示路径编码歧义。
    """
    import socket

    use_tls, host, port = _host_port(base_url)
    lines = [f"{method} {target} HTTP/1.1", f"Host: {host}"]
    if body:
        headers = {**headers, "Content-Length": str(len(body))}
    for name, value in headers.items():
        lines.append(f"{name}: {value}")
    lines.append("Connection: close")
    raw = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body

    if use_tls:  # 本项目只用 http，保留 https 的显式报错而非静默明文
        raise ValueError("https 不被示例客户端支持")
    with socket.create_connection((host, port), timeout=10) as sock:
        sock.sendall(raw)
        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    response = b"".join(chunks)
    head, _, payload = response.partition(b"\r\n\r\n")
    status_line = head.split(b"\r\n", 1)[0].decode("ascii", "replace")
    status = int(status_line.split(" ", 2)[1])
    return status, {}, payload


def _print_result(label: str, status: int, data: bytes) -> None:
    try:
        pretty = json.dumps(json.loads(data), ensure_ascii=False)
    except (ValueError, UnicodeDecodeError):
        pretty = data.decode("utf-8", "replace")
    mark = "OK " if 200 <= status < 300 else "!! "
    print(f"[{mark}{status}] {label}: {pretty}")


# ---------------------------------------------------------------- demo ----

def demo(args: argparse.Namespace) -> int:
    keys = load_keystore(args.keystore)
    key_id = sorted(keys)[0]
    key = keys[key_id]
    base = args.base_url
    method = "POST"
    target = "/api/data"
    body = json.dumps({"op": "transfer", "amount": 100}).encode("utf-8")

    print(f"== 使用 key_id={key_id}（密钥内容不会被打印），目标 {base}{target} ==\n")

    # 1) 正常请求
    headers, canonical = build_signed_headers(
        key=key, key_id=key_id, method=method, target=target, body=body
    )
    print("规范请求串（客户端侧）：")
    print("    " + canonical.replace("\n", "\n    "))
    status, _, data = send(base, method, target, headers, body)
    _print_result("正常请求（应 200）", status, data)

    # 2) 原样重放：同一时间戳 + 同一 nonce + 同一签名
    status, _, data = send(base, method, target, headers, body)
    _print_result("原样重放（应 409 replay_detected）", status, data)

    # 3) 并发重复：N 个线程发送同一条已签名请求，必须恰好一个 200
    same_headers, _ = build_signed_headers(
        key=key, key_id=key_id, method=method, target=target, body=body
    )
    results: list[int] = []
    barrier = threading.Barrier(args.concurrency)

    def worker() -> None:
        barrier.wait()  # 尽量让所有请求同时进入服务端
        st, _, _ = send(base, method, target, dict(same_headers), body)
        results.append(st)

    threads = [threading.Thread(target=worker) for _ in range(args.concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    accepted = sum(1 for st in results if st == 200)
    replayed = sum(1 for st in results if st == 409)
    print(f"[并发] {args.concurrency} 个完全相同的并发请求："
          f"{accepted} 个 200，{replayed} 个 409（必须恰好 1 个 200）")
    if accepted != 1 or replayed != args.concurrency - 1:
        print("       !! 并发去重失败")
        return 2

    # 4) 正文篡改：签名不变，只换正文一个字节
    tampered = body.replace(b"100", b"999", 1)
    status, _, data = send(base, method, target, headers, tampered)
    _print_result("正文篡改 100->999（应 401 bad_signature）", status, data)

    # 5) 时间窗口：真实网络下时间戳从生成到服务端处理有耗时，
    #    精确 ±300 边界由注入时钟的单元测试断言（见 tests/test_verifier.py）；
    #    这里演示 ±299（窗口内，通过）与 ±305（确定在窗口外，拒绝）。
    now = int(time.time())
    for label, offset, expect in [
        ("时间戳 now-299（窗口内，应 200）", -299, 200),
        ("时间戳 now+299（窗口内，应 200）", 299, 200),
        ("时间戳 now-305（应 401 stale）", -305, 401),
        ("时间戳 now+305（应 401 future）", 305, 401),
    ]:
        h, _ = build_signed_headers(
            key=key, key_id=key_id, method=method, target=target,
            body=body, timestamp=now + offset,
        )
        st, _, d = send(base, method, target, h, body)
        _print_result(label, st, d)
        if st != expect:
            print(f"       !! 时间边界不符合预期（期望 {expect}）")
            return 3

    # 6) 路径编码歧义：多种 raw target 必须等价并被接受
    variants = [
        "/api/data", "/./api/data", "//api//data",
        "/api/%64ata", "/api/../api/data", "/api/data?b=2&a=1",
    ]
    for raw in variants:
        h, _ = build_signed_headers(
            key=key, key_id=key_id, method=method, target=raw, body=body
        )
        st, _, d = send(base, method, raw, h, body)
        _print_result(f"等价路径 {raw}（应 200）", st, d)
        if st != 200:
            print("       !! 路径规范化误判")
            return 4

    # 7) 恶意路径：编码分隔符 / 逃出根 / 重复键。
    #    合规客户端在规范化阶段就拒绝；攻击者会绕过客户端直接构造签名，
    #    服务端仍必须独立拒绝（这里手工构造“攻击者签名”来证明）。
    from anti_replay.canonical import build_canonical_request
    from anti_replay.signing import sign_canonical

    now_ts = int(time.time())
    for raw in ["/api%2fdata", "/%2e%2e/etc/passwd", "/api/data?a=1&a=2"]:
        try:
            canonical_request_target(raw)
            raise AssertionError("合规客户端不应接受该 target")
        except CanonicalizationError:
            pass
        raw_path = raw.split("?", 1)[0]
        raw_query = raw.split("?", 1)[1] if "?" in raw else ""
        nonce = _new_nonce()
        cr = build_canonical_request(
            key_id=key_id, method=method,
            canonical_path_value=raw_path, canonical_query_value=raw_query,
            body=body, timestamp=str(now_ts), nonce=nonce,
        )
        evil_headers = {
            "Content-Type": "application/json",
            "X-Auth-Key-Id": key_id,
            "X-Auth-Timestamp": str(now_ts),
            "X-Auth-Nonce": nonce,
            "X-Auth-Signature": sign_canonical(cr, key),
        }
        st, _, d = send(base, method, raw, evil_headers, body)
        _print_result(f"恶意请求目标 {raw}（应 400）", st, d)
        if st != 400:
            print("       !! 恶意请求目标未被服务端拒绝")
            return 5

    # 8) 未知 key id
    h, _ = build_signed_headers(
        key=key, key_id="no-such-key", method=method, target=target, body=body
    )
    st, _, d = send(base, method, target, h, body)
    _print_result("未知 key id（应 401 unknown_key）", st, d)

    # 9) 健康检查
    st, _, d = send(base, "GET", "/health", {}, b"")
    _print_result("GET /health（应 200）", st, d)

    print("\n全部场景完成。")
    return 0


# ---------------------------------------------------------------- call ----

def call(args: argparse.Namespace) -> int:
    keys = load_keystore(args.keystore)
    try:
        key = keys[args.key_id]
    except KeyError:
        print(f"key id {args.key_id!r} 不在 keystore 中", file=sys.stderr)
        return 1

    body = args.body.encode("utf-8") if args.body is not None else b""
    headers, canonical = build_signed_headers(
        key=key, key_id=args.key_id, method=args.method.upper(),
        target=args.path, body=body,
        timestamp=args.timestamp, nonce=args.nonce,
    )
    if args.show_canonical:
        print(canonical)
    status, _, data = send(args.base_url, args.method.upper(), args.path, headers, body)
    _print_result(f"{args.method} {args.path}", status, data)
    return 0 if 200 <= status < 300 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="防重放客户端 / 演示")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_demo = sub.add_parser("demo", help="运行全部攻击场景演示")
    p_demo.add_argument("--base-url", default="http://127.0.0.1:8080")
    p_demo.add_argument("--keystore", required=True)
    p_demo.add_argument("--concurrency", type=int, default=32)
    p_demo.set_defaults(func=demo)

    p_call = sub.add_parser("call", help="发送单个签名请求")
    p_call.add_argument("--base-url", default="http://127.0.0.1:8080")
    p_call.add_argument("--keystore", required=True)
    p_call.add_argument("--key-id", default=None)
    p_call.add_argument("--method", default="POST")
    p_call.add_argument("--path", default="/api/data")
    p_call.add_argument("--body", default='{"hello":"world"}')
    p_call.add_argument("--timestamp", type=int, default=None)
    p_call.add_argument("--nonce", default=None)
    p_call.add_argument("--show-canonical", action="store_true")
    p_call.set_defaults(func=call)
    args = parser.parse_args(argv)

    if args.cmd == "call" and args.key_id is None:
        args.key_id = sorted(load_keystore(args.keystore))[0]
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
