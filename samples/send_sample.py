#!/usr/bin/env python3
"""把 samples/*.bin（已含 2 字节大端长度前缀）原样发给 dns-server，并打印应答。

用法：python3 samples/send_sample.py <样例.bin> [127.0.0.1:11053]
"""
import socket
import struct
import sys

path = sys.argv[1]
addr = sys.argv[2].rsplit(":", 1) if len(sys.argv) > 2 else ["127.0.0.1", "11053"]
host, port = addr[0], int(addr[1])

frame = open(path, "rb").read()
s = socket.create_connection((host, port), timeout=3)
s.sendall(frame)
try:
    hdr = s.recv(2)
    if len(hdr) < 2:
        print("服务端关闭连接，无应答（通常表示连 ID 都无法恢复的严重畸形）")
        sys.exit(0)
    (n,) = struct.unpack("!H", hdr)
    data = b""
    while len(data) < n:
        chunk = s.recv(n - len(data))
        if not chunk:
            break
        data += chunk
    print(f"应答 {len(data)} 字节: {data.hex()}")
    rcode = data[3] & 0x0F if len(data) >= 4 else None
    qr = (data[2] >> 7) & 1 if len(data) >= 4 else None
    print(f"QR={qr} RCODE={rcode}")
except socket.timeout:
    print("超时：服务端未应答")
finally:
    s.close()
