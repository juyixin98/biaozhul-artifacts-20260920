#!/usr/bin/env python3
"""生成“包含 multipart 边界前缀”的二进制测试载荷。

输出 examples/binary-payload.bin。载荷刻意塞入下列**近似边界**字节序列，
用于验证解析器不会把正文误判成分隔行：

  --<boundary>            裸边界前缀（缺前导 CRLF）
  --<boundary>-           像关闭标记但只有一个 '-'
  --<boundary>--          完整关闭标记，但缺前导 CRLF（前面是普通字节）
  \r\n--<boundary>-x      有前导 CRLF、边界正确，但后缀 '-x' 非法
  \r\n--<boundary>\rx     后缀是 CR 但第二个字节不是 LF

同时随机塞入 0x00 / 0xFF / 0xFE 等非文本字节，验证二进制安全。
末尾**不会**出现真分隔行，外层仍由调用方在其后追加 `\\r\\n--<boundary>--\\r\\n`。
"""

import hashlib
import os
import sys

BOUNDARY = os.environ.get("BOUNDARY", "B")
OUT = os.path.join(os.path.dirname(__file__), "binary-payload.bin")


def main() -> int:
    b = BOUNDARY.encode()
    parts = [
        b"binary-start\x00\xff\xfe",
        b"naked--" + b + b"--inside",        # 裸 --B / --B-- 嵌在字节中间
        b"fake-close\r\n--" + b + b"-x",     # 有 CRLF 但后缀 -x
        b"bad-cr\r\n--" + b + b"\rx",        # 有 CRLF 但 CR 后不是 LF
        b"one-dash\r\n--" + b + b"-",        # 只有一个 '-'，且恰好贴近片段末尾
        b"\x00" * 16 + b"tail\xff",
    ]
    data = b"".join(parts)
    # 安全断言：载荷自身不得包含真分隔行（否则就是测试数据写错了）
    assert b"\r\n--" + b + b"--" not in data
    assert b"\r\n--" + b + b"\r\n" not in data
    with open(OUT, "wb") as f:
        f.write(data)
    print(f"wrote {OUT} ({len(data)} bytes), sha256={hashlib.sha256(data).hexdigest()}")


if __name__ == "__main__":
    raise SystemExit(main())
