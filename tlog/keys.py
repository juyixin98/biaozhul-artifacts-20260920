"""测试密钥与 STH 签名（Ed25519，基于 cryptography 库）。

安全边界声明
============
* 仅用于本地演示 / 测试：密钥为每次本地生成的 **测试密钥**，不接入任何生产账号。
* 签名算法使用成熟库 ``cryptography`` 的 Ed25519（RFC 8032），不自创密码原语。
* 私钥以 PEM 文件落盘（PKCS8，未加密），仅适合本机临时数据目录；
  真实部署必须接入 KMS / HSM 并对私钥文件加密。

签名输入同样做了「域分离」：STH 的待签字节带有固定标签前缀
``TLOG STH v1``，把「树头签名」与其他可能的签名用途隔离开，
避免同密钥跨协议复用造成混淆。
"""

from __future__ import annotations

import struct

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

# STH 签名的域分离标签（前缀中含版本号，便于将来升级）。
STH_SIGNING_LABEL = b"tlog-sth-v1"

# 测试密钥文件的固定文件名（位于日志数据目录下）。
PRIVATE_KEY_FILENAME = "test_ed25519_private.pem"
PUBLIC_KEY_FILENAME = "test_ed25519_public.pem"


def generate_test_keypair() -> Ed25519PrivateKey:
    """生成一把全新的 Ed25519 测试密钥。"""
    return Ed25519PrivateKey.generate()


def save_private_key(key: Ed25519PrivateKey, path: str) -> None:
    """把私钥以未加密 PKCS8 PEM 写入文件（仅限本地测试用途）。"""
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    with open(path, "wb") as f:
        f.write(pem)


def load_private_key(path: str) -> Ed25519PrivateKey:
    """从 PEM 文件读取 Ed25519 私钥。"""
    with open(path, "rb") as f:
        key = serialization.load_pem_private_key(f.read(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise TypeError("私钥文件不是 Ed25519 密钥")
    return key


def save_public_key(key: Ed25519PublicKey, path: str) -> None:
    """把公钥以 SubjectPublicKeyInfo PEM 写入文件。"""
    pem = key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    with open(path, "wb") as f:
        f.write(pem)


def load_public_key(path: str) -> Ed25519PublicKey:
    """从 PEM 文件读取 Ed25519 公钥。"""
    with open(path, "rb") as f:
        key = serialization.load_pem_public_key(f.read())
    if not isinstance(key, Ed25519PublicKey):
        raise TypeError("公钥文件不是 Ed25519 密钥")
    return key


def public_key_raw(key: Ed25519PublicKey) -> bytes:
    """返回 32 字节原始公钥（用于在 JSON 中以 hex 展示）。"""
    return key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def encode_sth(
    tree_size: int,
    timestamp_ms: int,
    sha256_root_hash: bytes,
) -> bytes:
    """构造 STH 的确定性待签字节。

    布局（所有整数为大端）::

        b"tlog-sth-v1"        12 字节固定标签（域分离）
        u64 tree_size          8 字节
        u64 timestamp_ms       8 字节
        32 字节 SHA-256 树根

    标签之外各字段固定长度，不存在歧义解析。
    """
    if len(sha256_root_hash) != 32:
        raise ValueError("树根必须是 32 字节 SHA-256")
    return (
        STH_SIGNING_LABEL
        + struct.pack(">Q", tree_size)
        + struct.pack(">Q", timestamp_ms)
        + sha256_root_hash
    )


def sign_sth(
    key: Ed25519PrivateKey,
    tree_size: int,
    timestamp_ms: int,
    sha256_root_hash: bytes,
) -> bytes:
    """对 STH 元组做 Ed25519 签名，返回原始 64 字节签名。"""
    return key.sign(
        encode_sth(tree_size, timestamp_ms, sha256_root_hash)
    )


def verify_sth_signature(
    public_key: Ed25519PublicKey,
    tree_size: int,
    timestamp_ms: int,
    sha256_root_hash: bytes,
    signature: bytes,
) -> bool:
    """校验 STH 签名；任何失败（含篡改、错误公钥、畸形数据）都返回 False。"""
    try:
        public_key.verify(
            signature,
            encode_sth(tree_size, timestamp_ms, sha256_root_hash),
        )
        return True
    except Exception:
        return False
