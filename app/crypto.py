"""密码学原语：真实 Ed25519 签名/验签、规范 JSON 序列化、SHA-256。

所有签名与哈希都由 `cryptography` / hashlib 真实执行，无模拟。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

CANONICAL_DOMAIN = "recon/v1"


def canonical_json(obj: Any) -> bytes:
    """确定性序列化：键排序、紧凑分隔、不转义 unicode。

    采用 RFC 8785 的关键约束（按键 Unicode 码位排序、无空白、固定分隔符），
    足以保证签名/验签双方字节一致。只允许 JSON 可表达的基本类型。
    """
    return json.dumps(
        obj,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_canonical(obj: Any) -> str:
    """对规范化后的对象计算 SHA-256。"""
    return sha256_hex(canonical_json(obj))


# ---------------------------------------------------------------------------
# Ed25519 密钥与签名
# ---------------------------------------------------------------------------

def generate_private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def public_key_hex(private_key: Ed25519PrivateKey) -> str:
    return private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ).hex()


def load_public_key(public_key_hex_str: str) -> Ed25519PublicKey:
    try:
        raw = bytes.fromhex(public_key_hex_str)
    except (ValueError, TypeError) as exc:
        raise ValueError("feeder public key must be 64 hex chars") from exc
    if len(raw) != 32:
        raise ValueError("feeder public key must be 32 bytes (64 hex chars)")
    return Ed25519PublicKey.from_public_bytes(raw)


def signing_message(payload: dict[str, Any]) -> bytes:
    """对事件载荷生成待签名摘要：域分隔 + 规范JSON 的 SHA-256。"""
    digest = sha256_hex(canonical_json(payload)).encode("ascii")
    return CANONICAL_DOMAIN.encode("ascii") + b"|event|" + digest


def sign_payload(private_key: Ed25519PrivateKey, payload: dict[str, Any]) -> str:
    return private_key.sign(signing_message(payload)).hex()


def verify_signature(public_key_hex_str: str, payload: dict[str, Any], signature_hex: str) -> bool:
    """用登记公钥验签；任何异常都归为验签失败（False），不抛出。"""
    try:
        pub = load_public_key(public_key_hex_str)
        signature = bytes.fromhex(signature_hex)
    except (ValueError, TypeError):
        return False
    try:
        pub.verify(signature, signing_message(payload))
        return True
    except InvalidSignature:
        return False
