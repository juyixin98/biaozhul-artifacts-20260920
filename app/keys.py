"""Ed25519 密钥工具：生成、PEM/裸字节序列化、key_id 计算。

所有密钥均为本地临时生成的测试密钥，不涉及任何生产凭据。
"""
from __future__ import annotations

import hashlib

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

_PEM_ENCODING = serialization.Encoding.PEM
_RAW_ENCODING = serialization.Encoding.Raw


def generate_keypair() -> tuple[Ed25519PrivateKey, Ed25519PublicKey]:
    private_key = Ed25519PrivateKey.generate()
    return private_key, private_key.public_key()


def public_raw(public_key: Ed25519PublicKey) -> bytes:
    return public_key.public_bytes(
        _RAW_ENCODING, serialization.PublicFormat.Raw
    )


def public_from_raw(raw: bytes) -> Ed25519PublicKey:
    return Ed25519PublicKey.from_public_bytes(raw)


def key_id(public_key: Ed25519PublicKey) -> str:
    """key_id = sha256(Ed25519 裸公钥) 的十六进制。"""
    return hashlib.sha256(public_raw(public_key)).hexdigest()


def public_pem(public_key: Ed25519PublicKey) -> str:
    return public_key.public_bytes(
        _PEM_ENCODING, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode("ascii")


def private_pem(private_key: Ed25519PrivateKey) -> str:
    return private_key.private_bytes(
        _PEM_ENCODING,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")


def load_public_pem(data: str | bytes) -> Ed25519PublicKey:
    if isinstance(data, str):
        data = data.encode("ascii")
    key = serialization.load_pem_public_key(data)
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("公钥必须是 Ed25519 类型")
    return key


def load_private_pem(data: str | bytes) -> Ed25519PrivateKey:
    if isinstance(data, str):
        data = data.encode("ascii")
    key = serialization.load_pem_private_key(data, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("私钥必须是 Ed25519 类型")
    return key


def sign(private_key: Ed25519PrivateKey, message: bytes) -> bytes:
    return private_key.sign(message)


def verify(public_key: Ed25519PublicKey, signature: bytes, message: bytes) -> bool:
    try:
        public_key.verify(signature, message)
        return True
    except InvalidSignature:
        return False
