"""密码原语封装。

所有 HMAC / 哈希运算均委托给 ``cryptography`` 库；
密钥生成使用标准库 ``secrets``（OS CSPRNG）。不自创任何密码算法。
"""

from __future__ import annotations

import secrets

from cryptography.hazmat.primitives import hashes, hmac


def sha256_hex(data: bytes) -> str:
    """返回 ``data`` 的 SHA-256 摘要，小写十六进制。"""
    digest = hashes.Hash(hashes.SHA256())
    digest.update(data)
    return digest.finalize().hex()


def hmac_sha256(key: bytes, message: bytes) -> bytes:
    """计算 HMAC-SHA256 原始摘要。"""
    signer = hmac.HMAC(key, hashes.SHA256())
    signer.update(message)
    return signer.finalize()


def hmac_sha256_hex(key: bytes, message: bytes) -> str:
    """计算 HMAC-SHA256 并返回小写十六进制字符串。"""
    return hmac_sha256(key, message).hex()


def verify_hmac(key: bytes, message: bytes, signature_hex: str) -> bool:
    """恒定时间校验十六进制 HMAC 签名；格式非法直接返回 False。"""
    try:
        signature = bytes.fromhex(signature_hex)
    except ValueError:
        return False
    if len(signature) != 32:
        return False
    # HMAC.verify 使用恒定时间比较，失败抛 InvalidSignature。
    verifier = hmac.HMAC(key, hashes.SHA256())
    verifier.update(message)
    try:
        verifier.verify(signature)
    except Exception:
        return False
    return True


def generate_key(num_bytes: int = 32) -> bytes:
    """用 OS CSPRNG 生成新的 HMAC 密钥（默认 256 位）。"""
    if num_bytes < 16:
        raise ValueError("key too short; HMAC-SHA256 至少应使用 16 字节")
    return secrets.token_bytes(num_bytes)
