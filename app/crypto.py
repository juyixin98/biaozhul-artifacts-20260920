"""真实密码学操作: SHA-256 内容哈希、HMAC-SHA256 索引签名、常量时间比较。

不使用任何第三方加密库, 全部基于标准库 hashlib/hmac/secrets 真实执行。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from pathlib import Path

ALGO = "sha256"
KEY_BYTES = 32


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def content_id(prefix: str, data: bytes) -> str:
    """带类型前缀的内容标识, 例如 bag-<sha256>。"""
    return f"{prefix}-{sha256_bytes(data)}"


def hmac_sign(key: bytes, message: bytes) -> str:
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def hmac_verify(key: bytes, message: bytes, signature: str) -> bool:
    try:
        expected = bytes.fromhex(signature)
    except ValueError:
        return False
    return hmac.compare_digest(hmac.new(key, message, hashlib.sha256).digest(), expected)


def load_or_create_key(path: Path) -> bytes:
    """读取 HMAC 密钥; 不存在则真实生成 256 位随机密钥并落盘 (仅属主可读写)。"""
    if path.exists():
        return path.read_bytes()
    key = secrets.token_bytes(KEY_BYTES)
    path.write_bytes(key)
    path.chmod(0o600)
    return key
