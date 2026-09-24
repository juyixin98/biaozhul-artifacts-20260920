"""真实密码学操作：请求体 SHA-256 指纹与响应 HMAC-SHA256 签名。

所有函数均委托 Python 标准库 ``hashlib`` / ``hmac``（OpenSSL 后端），
不是占位实现。每个跟踪会话在创建时生成独立随机密钥（``secrets``，
CSPRNG），用于对响应做 HMAC，客户端可用创建会话时返回的密钥校验。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets


def new_session_key(n_bytes: int = 32) -> str:
    """用 CSPRNG 生成一个新的会话签名密钥（hex 编码）。"""
    return secrets.token_hex(n_bytes)


def sha256_hex(payload: dict) -> str:
    """对 JSON 可序列化对象计算稳定的 SHA-256 hex 指纹。

    键排序、紧凑分隔，保证语义相同的请求得到相同指纹，供幂等去重使用。
    """
    canonical = canonical_json(payload)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def canonical_json(payload: dict) -> str:
    """规范化 JSON 序列化（sort_keys、无多余空白、不转义非 ASCII）。"""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def sign_response(body: dict, key_hex: str) -> str:
    """对响应体计算 HMAC-SHA256（hex）。签名覆盖规范化后的完整 JSON。"""
    key = bytes.fromhex(key_hex)
    msg = canonical_json(body).encode("utf-8")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def verify_signature(body: dict, key_hex: str, signature_hex: str) -> bool:
    """常量时间比较校验 HMAC 签名。"""
    expected = sign_response(body, key_hex)
    return hmac.compare_digest(expected, signature_hex)
