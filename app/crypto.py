"""真实密码学操作: 事件与报告均使用 Ed25519 签名并实际验证。

签名报文 = 对载荷做规范化 JSON 序列化 (sort_keys, 无空白,
ensure_ascii=False, 无 key 排序歧义) 后取 UTF-8 字节, 再用
Ed25519 私钥签名 (RFC 8032)。签名以十六进制传输。

该规范化是确定性的: 相同逻辑结构必然得到相同字节串, 供重算去重。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PrivateFormat,
    PublicFormat,
    load_pem_private_key,
    load_pem_public_key,
)


def canonical_json(obj) -> bytes:
    """确定性 JSON 序列化, 返回 UTF-8 字节。"""
    return json.dumps(
        obj,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


# ---- 密钥生成与持久化 ----


def generate_private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def public_hex(priv: Ed25519PrivateKey) -> str:
    return priv.public_key().public_bytes(
        Encoding.Raw, PublicFormat.Raw
    ).hex()


def public_key_from_hex(hexkey: str) -> Ed25519PublicKey:
    return Ed25519PublicKey.from_public_bytes(bytes.fromhex(hexkey.strip()))


def save_private_key(priv: Ed25519PrivateKey, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        priv.private_bytes(
            encoding=Encoding.PEM,
            format=PrivateFormat.PKCS8,
            encryption_algorithm=_no_enc(),
        )
    )
    os.chmod(path, 0o600)


def load_private_key(path: Path) -> Ed25519PrivateKey:
    data = path.read_bytes()
    key = load_pem_private_key(data, password=None)
    assert isinstance(key, Ed25519PrivateKey)
    return key


def load_or_create_server_key(keys_dir: Path) -> Ed25519PrivateKey:
    """服务端报告签名密钥: 不存在则在本地生成 (仅离线回放使用)。"""
    path = keys_dir / "server_ed25519.pem"
    if path.exists():
        return load_private_key(path)
    key = generate_private_key()
    save_private_key(key, path)
    return key


def _no_enc():
    from cryptography.hazmat.primitives import serialization

    return serialization.NoEncryption()


# ---- 签名 / 验签 ----


def sign_payload(priv: Ed25519PrivateKey, payload: object) -> str:
    return priv.sign(canonical_json(payload)).hex()


def verify_payload(pub: Ed25519PublicKey, payload: object, signature_hex: str) -> bool:
    try:
        raw = bytes.fromhex(signature_hex)
    except (ValueError, TypeError):
        return False
    try:
        pub.verify(raw, canonical_json(payload))
        return True
    except InvalidSignature:
        return False
