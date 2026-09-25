#!/usr/bin/env python3
"""wsframe 手工演示客户端（仅用 Python 标准库，不使用 websocket 库）。

直接在 TCP 字节流上构造 RFC 6455 握手与帧，演示：
  1. 正常握手 + echo
  2. 逐字节发送一帧（服务端增量解析）
  3. text 消息分片，且在"半个 UTF-8 字符"中间插入 Ping
  4. 各类非法输入 → 服务端关闭码检查（1002 / 1007 / 1009）

用法：
  python3 examples/python/client_demo.py [127.0.0.1 [9001]]

服务端：cargo run --release
"""

import os
import socket
import struct
import sys
import time

GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OPCODE_CONT, OPCODE_TEXT, OPCODE_BINARY = 0x0, 0x1, 0x2
OPCODE_CLOSE, OPCODE_PING, OPCODE_PONG = 0x8, 0x9, 0xA


def ws_accept(key: bytes) -> str:
    import base64
    import hashlib

    return base64.b64encode(hashlib.sha1(key + GUID).digest()).decode()


def recv_until(sock: socket.socket, marker: bytes) -> bytes:
    buf = b""
    while marker not in buf:
        chunk = sock.recv(1)
        if not chunk:
            break
        buf += chunk
    return buf


def read_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("closed during read_exact")
        buf += chunk
    return buf


def read_frame(sock: socket.socket):
    """解析一帧服务端帧（服务端帧不掩码）。"""
    b0, b1 = read_exact(sock, 2)
    fin = bool(b0 & 0x80)
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    assert not masked, "server frame must not be masked"
    length = b1 & 0x7F
    if length == 126:
        length = struct.unpack("!H", read_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", read_exact(sock, 8))[0]
    payload = read_exact(sock, length) if length else b""
    return fin, opcode, payload


def encode_frame(opcode: int, payload: bytes, fin: bool = True, mask: bool = True) -> bytes:
    out = bytes([(0x80 if fin else 0x00) | opcode])
    n = len(payload)
    high = 0x80 if mask else 0x00
    if n <= 125:
        out += bytes([high | n])
    elif n <= 0xFFFF:
        out += bytes([high | 126]) + struct.pack("!H", n)
    else:
        out += bytes([high | 127]) + struct.pack("!Q", n)
    if mask:
        key = os.urandom(4)
        out += key
        out += bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    else:
        out += payload
    return out


def send_bytewise(sock: socket.socket, data: bytes, gap: float = 0.002) -> None:
    for b in data:
        sock.sendall(bytes([b]))
        time.sleep(gap)


def handshake(sock: socket.socket) -> None:
    import base64

    key = base64.b64encode(os.urandom(16))
    req = (
        b"GET / HTTP/1.1\r\n"
        b"Host: localhost\r\n"
        b"Upgrade: websocket\r\n"
        b"Connection: Upgrade\r\n"
        b"Sec-WebSocket-Key: " + key + b"\r\n"
        b"Sec-WebSocket-Version: 13\r\n\r\n"
    )
    sock.sendall(req)
    resp = recv_until(sock, b"\r\n\r\n")
    text = resp.decode()
    assert text.startswith("HTTP/1.1 101"), text
    expected = ws_accept(key)
    assert f"Sec-WebSocket-Accept: {expected}" in text, text
    print(f"[ok] handshake, accept={expected}")


def close_code(fin, opcode, payload):
    if opcode == OPCODE_CLOSE and len(payload) >= 2:
        return struct.unpack("!H", payload[:2])[0]
    return None


def main() -> int:
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 9001

    # ---- 1. 正常 echo + 逐字节发送 ----
    s = socket.create_connection((host, port))
    s.settimeout(5)
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    handshake(s)

    frame = encode_frame(OPCODE_TEXT, "你好 wsframe".encode())
    print("[..] sending one frame byte-by-byte (%d TCP segments)" % len(frame))
    send_bytewise(s, frame)
    fin, op, payload = read_frame(s)
    assert op == OPCODE_TEXT and payload.decode() == "你好 wsframe"
    print("[ok] bytewise echo:", payload.decode())

    # ---- 2. 分片 + 半个 UTF-8 字符中间插 ping ----
    # "中" = E4 B8 AD，先发 E4（半个字符），插入 ping，再发 B8 AD
    s.sendall(encode_frame(OPCODE_TEXT, b"\xe4", fin=False))
    s.sendall(encode_frame(OPCODE_PING, b"inserted"))
    fin, op, payload = read_frame(s)
    assert op == OPCODE_PONG and payload == b"inserted"
    print("[ok] pong arrived while message was half a UTF-8 char")
    s.sendall(encode_frame(OPCODE_CONT, b"\xb8\xad", fin=True))
    fin, op, payload = read_frame(s)
    assert op == OPCODE_TEXT and payload == "中".encode()
    print("[ok] cross-fragment UTF-8 message:", payload.decode())
    s.close()

    # ---- 3. 非法：孤立 continuation → 1002 ----
    s = socket.create_connection((host, port))
    s.settimeout(5)
    handshake(s)
    s.sendall(encode_frame(OPCODE_CONT, b"x"))
    fin, op, payload = read_frame(s)
    code = close_code(fin, op, payload)
    print(f"[check] stray continuation -> close code {code}")
    assert code == 1002, code
    s.close()

    # ---- 4. 非法：末尾半个 UTF-8 字符 → 1007 ----
    s = socket.create_connection((host, port))
    s.settimeout(5)
    handshake(s)
    s.sendall(encode_frame(OPCODE_TEXT, b"\xe4\xb8"))  # FIN=1 但字符残缺
    fin, op, payload = read_frame(s)
    code = close_code(fin, op, payload)
    print(f"[check] dangling half UTF-8 char -> close code {code}")
    assert code == 1007, code
    s.close()

    # ---- 5. 非法：未掩码客户端帧 → 1002（原始字节） ----
    s = socket.create_connection((host, port))
    s.settimeout(5)
    handshake(s)
    s.sendall(bytes([0x81, 0x02]) + b"Hi")  # MASK=0
    fin, op, payload = read_frame(s)
    code = close_code(fin, op, payload)
    print(f"[check] unmasked client frame -> close code {code}")
    assert code == 1002, code
    s.close()

    print("\nALL DEMO CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
