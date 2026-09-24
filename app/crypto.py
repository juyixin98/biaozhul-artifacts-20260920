"""密码学操作：Ed25519 签名与 SHA-256 摘要（真实执行，非占位）。

* 服务启动时从 ``keys/ed25519_private.pem`` 加载私钥；不存在则现场生成并
  以 PEM(PKCS8) 写盘（权限 0600），公钥随每次响应下发。
* 签名内容为审计结果的规范化 JSON（sort_keys、无多余空白），先算
  SHA-256 摘要入信封，再对同一字节串做 Ed25519 签名（base64）。
* ``/verify`` 用请求自带公钥验签，返回结构有效性，不依赖任何外部服务。

Ed25519 是确定性签名（RFC 8032），同一私钥+消息签名可复现；这里不用于
加密机密，只保证报告真实性与防篡改。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

KEY_DIR = Path(os.environ.get("CALIB_KEY_DIR", "keys"))
PRIVATE_KEY_PATH = KEY_DIR / "ed25519_private.pem"


def b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def b64d(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


def canonical_bytes(obj) -> bytes:
    """规范化 JSON：sort_keys、分隔符紧凑、ensure_ascii=False。"""
    return json.dumps(
        obj,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


class Signer:
    def __init__(self, private_key: Ed25519PrivateKey):
        self._private = private_key
        self._public = private_key.public_key()
        pub_raw = self._public.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        self.public_key_b64 = b64e(pub_raw)
        # key_id 取公钥 SHA-256 前 16 字节，便于密钥轮换识别
        self.key_id = "ed25519-" + hashlib.sha256(pub_raw).hexdigest()[:32]

    @classmethod
    def load_or_create(cls, path: Path = PRIVATE_KEY_PATH) -> "Signer":
        if path.exists():
            private = serialization.load_pem_private_key(
                path.read_bytes(), password=None
            )
            if not isinstance(private, Ed25519PrivateKey):
                raise TypeError("私钥不是 Ed25519")
            return cls(private)
        path.parent.mkdir(parents=True, exist_ok=True)
        private = Ed25519PrivateKey.generate()
        pem = private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        path.write_bytes(pem)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return cls(private)

    def sign_result(self, result_obj: dict) -> tuple[str, str, str]:
        """返回 (payload_sha256_hex, signature_b64, public_key_b64)。"""
        payload = canonical_bytes(result_obj)
        digest = hashlib.sha256(payload).hexdigest()
        sig = self._private.sign(payload)
        return digest, b64e(sig), self.public_key_b64


def verify_signature(
    public_key_b64: str, payload_obj: dict, signature_b64: str
) -> bool:
    """用给定公钥验证 ``canonical(payload_obj)`` 的 Ed25519 签名。"""
    try:
        pub_raw = b64d(public_key_b64)
        pub = Ed25519PublicKey.from_public_bytes(pub_raw)
        pub.verify(b64d(signature_b64), canonical_bytes(payload_obj))
        return True
    except Exception:
        return False


def sha256_hex(obj) -> str:
    return hashlib.sha256(canonical_bytes(obj)).hexdigest()
