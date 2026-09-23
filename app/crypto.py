"""规范化序列化、SHA-256 摘要与 Ed25519 签名/验签。

密码操作全部使用 cryptography 库真实执行，不存在占位实现。
"""
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


def canonical(obj: object) -> bytes:
    """确定性 JSON 序列化：键排序、无多余空白、不转义非 ASCII。"""
    return json.dumps(
        obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def sha256_hex(obj: object) -> str:
    return hashlib.sha256(canonical(obj)).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def generate_keypair() -> tuple[Ed25519PrivateKey, Ed25519PublicKey]:
    priv = Ed25519PrivateKey.generate()
    return priv, priv.public_key()


def public_pem(pub: Ed25519PublicKey) -> str:
    return pub.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")


def key_id(pub: Ed25519PublicKey) -> str:
    """key id = SHA-256(SPKI DER) 前 16 字节十六进制。"""
    der = pub.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return hashlib.sha256(der).hexdigest()[:32]


def sign(priv: Ed25519PrivateKey, message: bytes) -> bytes:
    return priv.sign(message)


def verify(pub: Ed25519PublicKey, signature: bytes, message: bytes) -> bool:
    try:
        pub.verify(signature, message)
        return True
    except InvalidSignature:
        return False


@dataclass(frozen=True)
class SigningIdentity:
    """签名身份：服务器自身密钥对 + 对外暴露的 key id / PEM。"""

    priv: Ed25519PrivateKey
    pub: Ed25519PublicKey

    @property
    def kid(self) -> str:
        return key_id(self.pub)

    def public_info(self) -> dict[str, str]:
        return {"kid": self.kid, "algorithm": "Ed25519", "public_key_pem": public_pem(self.pub)}

    def sign_object(self, obj: object) -> dict[str, str]:
        msg = canonical(obj)
        return {
            "kid": self.kid,
            "alg": "Ed25519",
            "sig": sign(self.priv, msg).hex(),
        }


def parse_public_pem(pem: str) -> Ed25519PublicKey:
    key = serialization.load_pem_public_key(pem.encode("ascii"))
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("仅支持 Ed25519 公钥")
    return key


def verify_object_signature(pub: Ed25519PublicKey, obj: object, sig_hex: str) -> bool:
    return verify(pub, bytes.fromhex(sig_hex), canonical(obj))
