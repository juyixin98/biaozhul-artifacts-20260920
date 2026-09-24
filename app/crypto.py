"""基于 ``cryptography`` 的 Ed25519 验签工具。

签名为可选能力：请求携带 ``X-Signature`` 头时才生效。
- 私钥永远只存在于客户端，本服务只做验签；
- ``/api/v1/keys/ed25519/generate`` 可生成测试用密钥对（生产应由用户自备）。
"""

from __future__ import annotations

from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .errors import SignatureError


@dataclass(frozen=True)
class GeneratedKey:
    public_key_pem: str
    private_key_pem: str
    key_id: str


def generate_keypair() -> GeneratedKey:
    private = Ed25519PrivateKey.generate()
    priv_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    pub_pem = private.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    raw_pub = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return GeneratedKey(
        public_key_pem=pub_pem, private_key_pem=priv_pem, key_id=raw_pub.hex()[:16]
    )


def _load_public_key(raw: str) -> Ed25519PublicKey:
    text = raw.strip()
    try:
        if "BEGIN PUBLIC KEY" in text:
            # HTTP 头不能含换行：允许客户端把 PEM 压成一行传入，这里还原
            body = text
            if "\n" not in body:
                body = body.replace("-----BEGIN PUBLIC KEY-----",
                                    "-----BEGIN PUBLIC KEY-----\n")
                body = body.replace("-----END PUBLIC KEY-----",
                                    "\n-----END PUBLIC KEY-----\n")
            loaded = serialization.load_pem_public_key(body.encode())
        else:
            loaded = serialization.load_der_public_key(bytes.fromhex(text))
    except Exception as exc:  # 格式/解码错误统一为签名错误
        raise SignatureError("bad_public_key", "公钥格式无法解析（需要 PEM 或 DER hex）") from exc
    if not isinstance(loaded, Ed25519PublicKey):
        raise SignatureError("bad_public_key", "仅支持 Ed25519 公钥")
    return loaded


def _load_signature(raw: str) -> bytes:
    try:
        sig = bytes.fromhex(raw.strip())
    except ValueError as exc:
        raise SignatureError("bad_signature", "签名必须是 64 字节 Ed25519 签名的 hex 编码") from exc
    if len(sig) != 64:
        raise SignatureError("bad_signature", f"签名长度应为 64 字节，实际 {len(sig)} 字节")
    return sig


def verify_signature(public_key: str | None, signature_hex: str, message: bytes) -> bool:
    """验证 ``signature_hex`` 是 ``public_key`` 对 ``message`` 的 Ed25519 签名。"""
    if not signature_hex:
        raise SignatureError("missing_signature", "缺少 X-Signature 头")
    if not public_key:
        raise SignatureError(
            "missing_public_key",
            "携带签名时必须同时提供 X-Public-Key（PEM 或 DER hex）",
        )
    key = _load_public_key(public_key)
    signature = _load_signature(signature_hex)
    try:
        key.verify(signature, message)
    except InvalidSignature as exc:
        raise SignatureError("invalid_signature", "签名校验失败") from exc
    return True


def sign(private_key_pem: str, message: bytes) -> str:
    """供测试/示例客户端使用：用 PKCS#8 PEM 私钥签名，返回 hex。"""
    key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise SignatureError("bad_public_key", "私钥不是 Ed25519")
    return key.sign(message).hex()
