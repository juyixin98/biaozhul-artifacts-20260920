#!/usr/bin/env python3
"""生成 samples/sample_request.bin：一个含二进制正文的 multipart 测试请求。

线协议：第一行 `BOUNDARY <boundary>`，随后是 multipart 消息体。
二进制正文故意包含边界前缀和近似边界，用于验证解析器不会误判。
"""
import os
import sys

BOUNDARY = "sampleBoundary-9"

body_binary = (
    b"\x00\x01\x02\xff\xfe"                       # 原始二进制
    b"\r\n--sampleBoundary-"                      # 边界前缀（缺最后字符）
    b"\r\n--sampleBoundary-9!"                    # 完整边界 + 非法后续字节
    b"middle--sampleBoundary-9"                   # 行中间的边界（前面无 CRLF）
    + bytes(range(256))                           # 全字节值
    + b"\r\n--sampleBoundary-9\rX"                # 边界 + \rX（非 \r\n）
)

msg = b"".join([
    f"BOUNDARY {BOUNDARY}\r\n".encode(),
    f"--{BOUNDARY}\r\n".encode(),
    b'Content-Disposition: form-data; name="title"\r\n',
    b"\r\n",
    "multipart 流式测试".encode("utf-8"),
    b"\r\n",
    f"--{BOUNDARY}\r\n".encode(),
    b'Content-Disposition: form-data; name="blob"; filename="blob.bin"\r\n',
    b"Content-Type: application/octet-stream\r\n",
    b"\r\n",
    body_binary,
    b"\r\n",
    f"--{BOUNDARY}\r\n".encode(),
    b'Content-Disposition: form-data; name="empty"\r\n',
    b"\r\n",
    b"\r\n",                                       # 空部件（零字节正文）
    f"--{BOUNDARY}--\r\n".encode(),
])

out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(__file__), "..", "samples", "sample_request.bin")
os.makedirs(os.path.dirname(out), exist_ok=True)
with open(out, "wb") as f:
    f.write(msg)
print(f"wrote {out} ({len(msg)} bytes)")
