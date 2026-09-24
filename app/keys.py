"""Ed25519 密钥管理。

私钥只应留在服务端；验证者通过带外渠道获得公钥并自行固定（pinning），
公钥即信任锚的一部分。服务虽然提供 /public_key 便于演示，
生产环境中验证者不应在验证时从被审计方拉取公钥。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

PRIVATE_KEY_FILE = "private_key.pem"
PUBLIC_KEY_FILE = "public_key.pem"


def ensure_keypair(key_dir: str | Path) -> tuple[Ed25519PrivateKey, str]:
    """加载或生成密钥对，返回 (私钥, key_id)。key_id 为公钥哈希前 16 位。"""
    key_dir = Path(key_dir)
    key_dir.mkdir(parents=True, exist_ok=True)
    priv_path = key_dir / PRIVATE_KEY_FILE
    pub_path = key_dir / PUBLIC_KEY_FILE

    if priv_path.exists():
        private_key = serialization.load_pem_private_key(
            priv_path.read_bytes(), password=None
        )
        if not isinstance(private_key, Ed25519PrivateKey):
            raise TypeError(f"{priv_path} 不是 Ed25519 私钥")
    else:
        private_key = Ed25519PrivateKey.generate()
        priv_path.write_bytes(
            private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        priv_path.chmod(0o600)

    public_key = private_key.public_key()
    pub_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    if not pub_path.exists() or pub_path.read_bytes() != pub_pem:
        pub_path.write_bytes(pub_pem)

    key_id = hashlib.sha256(pub_pem).hexdigest()[:16]
    return private_key, key_id


def load_public_key(pem: str | bytes) -> Ed25519PublicKey:
    """从 PEM 加载验证者持有的公钥（信任锚）。"""
    if isinstance(pem, str):
        pem = pem.encode("utf-8")
    key = serialization.load_pem_public_key(pem)
    if not isinstance(key, Ed25519PublicKey):
        raise TypeError("公钥不是 Ed25519")
    return key


def public_key_pem(private_key: Ed25519PrivateKey) -> str:
    return (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("utf-8")
    )
