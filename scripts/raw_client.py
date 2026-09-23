#!/usr/bin/env python3
"""用裸 TCP 手工构造 WebSocket 帧，验证服务端的协议错误处理。

用法: python3 scripts/raw_client.py <case>
case: unmasked | oversize | badutf8 | half-rune | fragmented-control | echo
"""
import base64
import os
import socket
import struct
import sys

HOST, PORT = "127.0.0.1", int(sys.argv[2]) if len(sys.argv) > 2 else 18083
CASE = sys.argv[1] if len(sys.argv) > 1 else "echo"


def handshake(sock):
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        "GET /ws HTTP/1.1\r\n"
        "Host: x\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    )
    sock.sendall(req.encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        resp += sock.recv(4096)
    assert b"101 Switching Protocols" in resp, resp


def frame(opcode, payload, fin=True, masked=True, rsv=0):
    b0 = (0x80 if fin else 0) | (rsv & 0x70) | (opcode & 0x0F)
    mask_bit = 0x80 if masked else 0x00
    n = len(payload)
    if n <= 125:
        header = bytes([b0, mask_bit | n])
    elif n <= 0xFFFF:
        header = bytes([b0, mask_bit | 126]) + struct.pack(">H", n)
    else:
        header = bytes([b0, mask_bit | 127]) + struct.pack(">Q", n)
    if masked:
        key = os.urandom(4)
        masked_payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
        return header + key + masked_payload
    return header + payload


def read_one_frame(sock):
    """读一个（未掩码的）服务端帧，返回 (opcode, payload)。"""
    hdr = recvn(sock, 2)
    if not hdr:
        return None, None
    fin = hdr[0] & 0x80
    opcode = hdr[0] & 0x0F
    length = hdr[1] & 0x7F
    if length == 126:
        length = struct.unpack(">H", recvn(sock, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", recvn(sock, 8))[0]
    payload = recvn(sock, length) if length else b""
    return opcode, payload


def recvn(sock, n):
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            break
        data += chunk
    return data


def main():
    sock = socket.create_connection((HOST, PORT), timeout=3)
    handshake(sock)

    if CASE == "unmasked":
        # 未掩码文本帧 -> 服务端必须 1002
        sock.sendall(frame(0x1, b"hi", masked=False))
    elif CASE == "oversize":
        # 超过默认 4MiB 消息上限的单帧 -> 1009
        sock.sendall(frame(0x2, b"\x00" * (4 * 1024 * 1024 + 1)))
    elif CASE == "badutf8":
        # 0xFF 是非法 UTF-8 起始字节 -> 1007
        sock.sendall(frame(0x1, b"ok\xff"))
    elif CASE == "half-rune":
        # 文本消息以半个 3 字节字符结束（E4 BD 缺 A0）-> 1007
        sock.sendall(frame(0x1, b"\xe4\xbd"))
    elif CASE == "fragmented-control":
        # 分片的 Ping（FIN=0）-> 1002
        sock.sendall(frame(0x9, b"x", fin=False))
    elif CASE == "echo":
        sock.sendall(frame(0x1, "你好".encode()))
        opcode, payload = read_one_frame(sock)
        print(f"echo opcode={opcode} payload={payload!r}")
        sock.sendall(frame(0x8, struct.pack(">H", 1000)))
        opcode, payload = read_one_frame(sock)
        print(f"close ack opcode={opcode} code={struct.unpack('>H', payload[:2])[0]}")
        return
    else:
        raise SystemExit(f"unknown case: {CASE}")

    opcode, payload = read_one_frame(sock)
    code = struct.unpack(">H", payload[:2])[0] if payload and len(payload) >= 2 else None
    print(f"case={CASE}: server frame opcode=0x{opcode:x} close_code={code}")
    assert opcode == 0x8, "expected close frame"


if __name__ == "__main__":
    main()
