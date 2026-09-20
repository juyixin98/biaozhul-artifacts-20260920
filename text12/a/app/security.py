from __future__ import annotations

import hashlib
import secrets


def generate_token(prefix: str = "tok") -> str:
    """生成不透明的 API Key / 设备令牌（仅展示一次，服务端只存哈希）。"""
    return f"{prefix}_{secrets.token_urlsafe(32)}"


def hash_token(token: str) -> str:
    """令牌做 SHA-256 存储。令牌由 secrets.token_urlsafe 生成，具备足够熵，
    无需慢哈希；恒定时间比较由 hmac.compare_digest 在认证层完成。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
