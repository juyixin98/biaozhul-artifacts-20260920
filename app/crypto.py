# -*- coding: utf-8 -*-
"""真实密码学操作：SHA-256 与 Ed25519 签名/验签。

教学说明：
- 信任根（root of trust）是创建轻客户端时登记的对端链 Ed25519 公钥；
- 每条链对每个已提交区块生成一个 *检查点*（高度、时间、应用状态根），
  并对检查点字节做真实 Ed25519 签名；
- 中继器（relayer）提交证明时必须附上检查点，状态机先验签名再验状态证明。
所有签名/验签都由 `cryptography` 库真实执行，没有任何模拟占位。
"""
from __future__ import annotations

import hashlib

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .encoding import canonical_json, from_hex, to_hex


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


# ---------------------------------------------------------------------------
# IBC v1 承诺的字节组装（真实协议规则）
#   commit_packet      = sha256(timeout_height_revision_number ||
#                               timeout_height_revision_height ||
#                               timeout_timestamp || sha256(data))
#   commit_ack         = sha256(sha256(ack))
#   不存在值时用全零哈希 ZERO
# ---------------------------------------------------------------------------

ZERO = b"\x00" * 32


def packet_commitment_bytes(
    timeout_revision_number: int,
    timeout_revision_height: int,
    timeout_timestamp_nanos: int,
    data: bytes,
) -> bytes:
    from .encoding import u64be

    return (
        u64be(timeout_revision_number)
        + u64be(timeout_revision_height)
        + u64be(timeout_timestamp_nanos)
        + sha256(data)
    )


def packet_commitment(
    timeout_revision_number: int,
    timeout_revision_height: int,
    timeout_timestamp_nanos: int,
    data: bytes,
) -> bytes:
    return sha256(
        packet_commitment_bytes(
            timeout_revision_number,
            timeout_revision_height,
            timeout_timestamp_nanos,
            data,
        )
    )


def ack_commitment(ack: bytes) -> bytes:
    return sha256(sha256(ack))


# ---------------------------------------------------------------------------
# 检查点（checkpoint）——对端链上状态根的签名证书
# ---------------------------------------------------------------------------

CHECKPOINT_VERSION = 1


def checkpoint_sign_bytes(cp: dict) -> bytes:
    """检查点的待签名字节：规范 JSON 后加固定域分隔前缀，避免歧义。"""
    payload = canonical_json(cp)
    return b"ibc-mini/checkpoint/v1\n" + payload


def generate_keypair() -> tuple[Ed25519PrivateKey, Ed25519PublicKey]:
    sk = Ed25519PrivateKey.generate()
    return sk, sk.public_key()


def public_key_hex(pk: Ed25519PublicKey) -> str:
    return to_hex(
        pk.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    )


def public_key_from_hex(h: str) -> Ed25519PublicKey:
    return Ed25519PublicKey.from_public_bytes(from_hex(h, "public_key"))


def private_key_hex(sk: Ed25519PrivateKey) -> str:
    return to_hex(
        sk.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )


def private_key_from_hex(h: str) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(from_hex(h, "private_key"))


def sign(sk: Ed25519PrivateKey, message: bytes) -> bytes:
    return sk.sign(message)


def verify_signature(public_key: Ed25519PublicKey | bytes, signature: bytes, message: bytes) -> bool:
    """验签，任何失败均返回 False（不抛异常），由调用方决定如何拒绝。"""
    try:
        if isinstance(public_key, bytes):
            public_key = Ed25519PublicKey.from_public_bytes(public_key)
        public_key.verify(signature, message)
        return True
    except (InvalidSignature, ValueError):
        return False
