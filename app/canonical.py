"""规范化序列化与哈希。

所有进入哈希链 / 签名的对象都必须先经过 canonical_json()：
- 键按字典序排序
- 无空白分隔符
- UTF-8 编码，不做 ASCII 转义
这样同一逻辑对象在任何机器上都得到唯一字节串。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(obj: Any) -> bytes:
    """对象的规范化 JSON 字节表示（进入哈希/签名的唯一形式）。"""
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    """带算法前缀的 SHA-256 十六进制摘要。"""
    return "sha256:" + hashlib.sha256(data).hexdigest()


def hash_object(obj: Any) -> str:
    """对任意可 JSON 序列化对象做规范化哈希。"""
    return sha256_hex(canonical_json(obj))
