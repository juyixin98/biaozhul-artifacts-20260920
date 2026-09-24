"""Ed25519 签名原语与 TUF 风格的 keyid 计算。

* 元数据结构为 ``{"signed": <负载>, "signatures": [{"keyid": ..., "sig": <hex>}]}``；
* keyid 是公钥对象 ``{"keytype": "ed25519", "scheme": "ed25519",
  "keyval": {"public": <hex 公钥>}}`` 的规范化 JSON 的 sha256 摘要；
* 签名内容是 ``signed`` 段落的规范化 JSON 字节串。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from . import canonical as canonical_mod
from .errors import MetadataError, SignatureError

KEYTYPE = "ed25519"
SCHEME = "ed25519"


@dataclass(frozen=True)
class KeyPair:
    """一对 Ed25519 密钥；:meth:`public_hex` 是 TUF keyval 里的公钥。"""

    private: Ed25519PrivateKey

    @classmethod
    def generate(cls) -> "KeyPair":
        return cls(Ed25519PrivateKey.generate())

    def public_hex(self) -> str:
        raw = self.private.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return raw.hex()

    def keyid(self) -> str:
        return keyid_for_public_hex(self.public_hex())

    def sign(self, data: bytes) -> str:
        return self.private.sign(data).hex()


def _public_key_object(public_hex: str) -> dict:
    return {
        "keytype": KEYTYPE,
        "scheme": SCHEME,
        "keyval": {"public": public_hex},
    }


def keyid_for_public_hex(public_hex: str) -> str:
    """按 TUF 约定计算公钥 keyid。"""

    if not _is_lower_hex(public_hex, 32):
        raise MetadataError("ed25519 公钥必须是 32 字节的十六进制字符串")
    return hashlib.sha256(
        canonical_mod.canonical(_public_key_object(public_hex))
    ).hexdigest()


def _load_public(public_hex: str) -> Ed25519PublicKey:
    try:
        return Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_hex))
    except (ValueError, TypeError) as exc:
        raise MetadataError("无法解析 ed25519 公钥") from exc


def _is_lower_hex(text: str, length: int) -> bool:
    if len(text) != length * 2:
        return False
    try:
        value = bytes.fromhex(text)
    except ValueError:
        return False
    return len(value) == length and text == text.lower()


def verify_signature(public_hex: str, signature_hex: str, data: bytes) -> None:
    """验证单条签名，失败时抛出 :class:`SignatureError`。"""

    if not _is_lower_hex(signature_hex, 64):
        raise SignatureError("签名必须是 64 字节的十六进制字符串")
    public = _load_public(public_hex)
    try:
        public.verify(bytes.fromhex(signature_hex), data)
    except InvalidSignature as exc:
        raise SignatureError("Ed25519 签名验证失败") from exc
