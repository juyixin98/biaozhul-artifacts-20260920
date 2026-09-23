#!/usr/bin/env python3
"""ws_probe.py — wsecho 的裸 TCP 探针客户端（仅用标准库）。

用途：
  * 作为「请求样例」与验收脚本，真实连接 wsecho 并逐字节发送手工构造的
    WebSocket 客户端帧（带掩码），解析服务端（未掩码）响应，打印关闭码；
  * 不依赖任何第三方 WebSocket 库，所有帧都按 RFC 6455 手工拼/拆。

握手：默认发送 RFC 6455 升级请求；--raw 时跳过握手（配合 wsecho --raw）。

示例：
  python3 scripts/ws_probe.py --raw --case ping-insert --byte-by-byte
  python3 scripts/ws_probe.py --case echo --byte-by-byte
"""

import argparse
import base64
import os
import socket
import struct
import sys
import time

OP_CONT, OP_TEXT, OP_BINARY = 0x0, 0x1, 0x2
OP_CLOSE, OP_PING, OP_PONG = 0x8, 0x9, 0xA
OP_NAMES = {0: "continuation", 1: "text", 2: "binary",
            8: "close", 9: "ping", 10: "pong"}

CLOSE_NAMES = {
    1000: "normal", 1001: "going-away", 1002: "protocol-error",
    1003: "unsupported-data", 1007: "invalid-payload-data",
    1008: "policy-violation", 1009: "message-too-big", 1011: "internal-error",
}


def encode_frame(fin, opcode, payload: bytes, mask: bool = True) -> bytes:
    """构造客户端帧（默认带掩码）。"""
    b0 = (0x80 if fin else 0x00) | (opcode & 0x0F)
    n = len(payload)
    if n <= 125:
        lenbytes = bytes([n])
    elif n <= 0xFFFF:
        lenbytes = bytes([126]) + struct.pack(">H", n)
    else:
        lenbytes = bytes([127]) + struct.pack(">Q", n)
    if mask:
        key = os.urandom(4)
        masked = bytes(c ^ key[i % 4] for i, c in enumerate(payload))
        return bytes([b0, lenbytes[0] | 0x80]) + lenbytes[1:] + key + masked
    return bytes([b0, lenbytes[0]]) + lenbytes[1:] + payload


def send(sock: socket.socket, data: bytes, byte_by_byte: bool):
    if byte_by_byte:
        for c in data:
            sock.sendall(bytes([c]))
            time.sleep(0.001)  # 放大增量解析路径，便于观察
    else:
        sock.sendall(data)


def recv_exact(sock, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("server closed")
        buf += chunk
    return buf


def read_frame(sock: socket.socket):
    """读取一个服务端帧（未掩码），返回 dict；连接关闭时返回 None。"""
    try:
        b0 = recv_exact(sock, 1)[0]
        b1 = recv_exact(sock, 1)[0]
    except (ConnectionError, OSError):
        return None
    fin = bool(b0 & 0x80)
    opcode = b0 & 0x0F
    assert not (b1 & 0x80), "server frame unexpectedly masked"
    ln = b1 & 0x7F
    if ln == 126:
        ln = struct.unpack(">H", recv_exact(sock, 2))[0]
    elif ln == 127:
        ln = struct.unpack(">Q", recv_exact(sock, 8))[0]
    payload = recv_exact(sock, ln) if ln else b""
    result = {"fin": fin, "opcode": opcode, "payload": payload}
    if opcode == OP_CLOSE and len(payload) >= 2:
        result["code"] = struct.unpack(">H", payload[:2])[0]
        result["reason"] = payload[2:]
    return result


def show_frame(label: str, f):
    if f is None:
        print(f"  <- {label}: <connection closed by server>")
        return
    name = OP_NAMES.get(f["opcode"], f"opcode-0x{f['opcode']:X}")
    extra = ""
    if "code" in f:
        cn = CLOSE_NAMES.get(f["code"], "unknown/private")
        reason = f["reason"].decode("utf-8", "replace")
        extra = f" code={f['code']} ({cn}) reason={reason!r}"
    data = f["payload"]
    shown = data if len(data) <= 24 else data[:24] + b"..."
    print(f"  <- {label}: fin={int(f['fin'])} {name} len={len(data)}{extra} bytes={shown!r}")


def do_handshake(sock: socket.socket):
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        "GET /chat HTTP/1.1\r\n"
        "Host: 127.0.0.1\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    )
    sock.sendall(req.encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("server closed during handshake")
        resp += chunk
    head = resp.split(b"\r\n\r\n", 1)[0].decode()
    print("  handshake response:")
    for line in head.split("\r\n"):
        print("    " + line)
    assert "101 Switching Protocols" in head, "expected 101"
    # 校验 Accept
    import hashlib
    expected = base64.b64encode(
        hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
    ).decode()
    assert f"Sec-WebSocket-Accept: {expected}" in head, "bad accept"
    print("  handshake OK, Sec-WebSocket-Accept verified")


# ---------------------------------------------------------------------------
# 各验收场景：返回 True 表示观察到预期的关闭码/响应
# ---------------------------------------------------------------------------

def case_echo(sock, bbb, connect=None):
    send(sock, encode_frame(True, OP_TEXT, "Hello, 世界!".encode()), bbb)
    show_frame("echo", read_frame(sock))
    return True


def case_ping_insert(sock, bbb, connect=None):
    # text! + ping + pong + text. 全为 fin
    send(sock, encode_frame(True, OP_TEXT, "one".encode()), bbb)
    show_frame("echo-1", read_frame(sock))
    send(sock, encode_frame(True, OP_PING, b"tick"), bbb)
    show_frame("pong", read_frame(sock))

    # 分片消息中插入 ping
    send(sock, encode_frame(False, OP_TEXT, "Hel".encode()), bbb)
    send(sock, encode_frame(True, OP_PING, b"mid-frag"), bbb)
    show_frame("pong-mid-fragment", read_frame(sock))
    send(sock, encode_frame(True, OP_CONT, b"lo"), bbb)
    show_frame("echo-reassembled", read_frame(sock))
    return True


def case_illegal_cont(sock, bbb, connect=None):
    # 没有起始帧直接 continuation
    send(sock, encode_frame(True, OP_CONT, b"x"), bbb)
    f = read_frame(sock)
    show_frame("close", f)
    return f and f.get("code") == 1002


def case_double_start(sock, bbb, connect=None):
    send(sock, encode_frame(False, OP_TEXT, b"ab"), bbb)
    send(sock, encode_frame(True, OP_TEXT, b"cd"), bbb)  # 分片中再 start
    f = read_frame(sock)
    show_frame("close", f)
    return f and f.get("code") == 1002


def case_half_utf8_valid(sock, bbb, connect=None):
    # “你” E4 BD A0 切成 2 + 1，跨帧合法
    send(sock, encode_frame(False, OP_TEXT, bytes([0xE4, 0xBD])), bbb)
    send(sock, encode_frame(True, OP_CONT, bytes([0xA0])), bbb)
    f = read_frame(sock)
    show_frame("echo", f)
    return f and f["payload"].decode() == "你"


def case_half_utf8_truncated(sock, bbb, connect=None):
    # fin 停在半个字符 -> 1007
    send(sock, encode_frame(True, OP_TEXT, bytes([0xE4, 0xBD])), bbb)
    f = read_frame(sock)
    show_frame("close", f)
    return f and f.get("code") == 1007


def case_bad_utf8_byte(sock, bbb, connect=None):
    send(sock, encode_frame(False, OP_TEXT, b"a"), bbb)
    send(sock, encode_frame(False, OP_CONT, b"\xFF"), bbb)
    f = read_frame(sock)
    show_frame("close", f)
    return f and f.get("code") == 1007


def case_too_big(sock, bbb, connect=None):
    # 默认消息上限 64KiB：发 70000 字节单帧（帧上限 1MiB 允许）
    send(sock, encode_frame(True, OP_BINARY, b"\x00" * 70_000), False)
    f = read_frame(sock)
    show_frame("close", f)
    return f and f.get("code") == 1009


def case_control_too_long(sock, bbb, connect=None):
    # ping 声明 126 字节载荷，只发帧头即可（服务端帧头层拒绝）
    hdr = bytes([0x89, 0xFE]) + struct.pack(">H", 126) + os.urandom(4)
    send(sock, hdr, False)
    f = read_frame(sock)
    show_frame("close", f)
    return f and f.get("code") == 1002


def case_reserved_opcode(sock, bbb, connect=None):
    send(sock, bytes([0x83, 0x80]) + os.urandom(4), False)  # fin opcode=3
    f = read_frame(sock)
    show_frame("close", f)
    return f and f.get("code") == 1002


def case_unmasked(sock, bbb, connect=None):
    send(sock, bytes([0x81, 0x00]), False)  # fin text, 未掩码, len 0
    f = read_frame(sock)
    show_frame("close", f)
    return f and f.get("code") == 1002


def case_close_codes(_sock, bbb, connect=None):
    # 关闭握手会终结 TCP 连接（§5.5.1），不同关闭码必须用不同连接验证。
    ok = True
    for code in (1000, 3000):
        with connect() as sock:
            send(sock, encode_frame(True, OP_CLOSE,
                                   struct.pack(">H", code) + b"bye"), False)
            f = read_frame(sock)
            show_frame(f"close-{code}", f)
            ok = ok and f and f.get("code") == code and f["reason"] == b"bye"
    return ok


def case_reserved_close_code(sock, bbb, connect=None):
    send(sock, encode_frame(True, OP_CLOSE, struct.pack(">H", 1005)), False)
    f = read_frame(sock)
    show_frame("close", f)
    return f and f.get("code") == 1002


CASES = {
    "echo": case_echo,
    "ping-insert": case_ping_insert,
    "illegal-cont": case_illegal_cont,
    "double-start": case_double_start,
    "half-utf8-valid": case_half_utf8_valid,
    "half-utf8-truncated": case_half_utf8_truncated,
    "bad-utf8-byte": case_bad_utf8_byte,
    "too-big": case_too_big,
    "control-too-long": case_control_too_long,
    "reserved-opcode": case_reserved_opcode,
    "unmasked": case_unmasked,
    "close-codes": case_close_codes,
    "reserved-close-code": case_reserved_close_code,
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9001)
    ap.add_argument("--raw", action="store_true", help="skip HTTP handshake")
    ap.add_argument("--byte-by-byte", action="store_true",
                    help="send each frame one byte at a time (with small delay)")
    ap.add_argument("--case", choices=sorted(CASES), default="echo")
    args = ap.parse_args()

    # connect() 返回一条已（按需）完成握手的新连接；关闭码类用例需要多条连接。
    def connect():
        sock = socket.create_connection((args.host, args.port), timeout=5)
        if not args.raw:
            do_handshake(sock)
        return sock

    print(f"connected to {args.host}:{args.port} case={args.case} "
          f"byte_by_byte={args.byte_by_byte} raw={args.raw}")
    with connect() as sock:
        ok = CASES[args.case](sock, args.byte_by_byte, connect=connect)
    print(f"RESULT: {'PASS' if ok else 'FAIL'}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
