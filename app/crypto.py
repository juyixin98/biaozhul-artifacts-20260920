"""密码学操作：Ed25519 真实签名，以及确定性域分离的投票消息编码。

所有字节拼接均使用显式长度前缀，杜绝歧义；不涉及任何浮点。
"""
from __future__ import annotations

import hashlib

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

DOMAIN_VOTE = b"p007-finality-vote-v1"


def _lp(data: bytes) -> bytes:
    """8 字节大端长度前缀 + 数据。"""
    return len(data).to_bytes(8, "big") + data


def vote_message(epoch: int, block_hash: str) -> bytes:
    """构造待签名的规范投票消息。

    格式：DOMAIN || len(epoch_bytes) || epoch_bytes || len(block_bytes) || block_bytes
    epoch 用大端整数编码（去掉前导零），block_hash 以 UTF-8 字节参与。
    """
    if epoch < 0:
        raise ValueError("epoch 必须为非负整数")
    epoch_bytes = int(epoch).to_bytes(
        max(1, (int(epoch).bit_length() + 7) // 8), "big"
    )
    block_bytes = block_hash.encode("utf-8")
    return _lp(DOMAIN_VOTE) + _lp(epoch_bytes) + _lp(block_bytes)


def generate_private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def derive_private_key(seed_material: bytes) -> Ed25519PrivateKey:
    """从种子材料确定性派生 Ed25519 私钥（仅用于示例/测试的可复现数据）。"""
    return Ed25519PrivateKey.from_private_bytes(hashlib.sha256(seed_material).digest())


def private_key_from_bytes(raw: bytes) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(raw)


def public_key_bytes(private_key: Ed25519PrivateKey) -> bytes:
    return private_key.public_key().public_bytes_raw()


def public_key_hex(private_key: Ed25519PrivateKey) -> str:
    return public_key_bytes(private_key).hex()


def _load_public_key(pub_hex: str) -> Ed25519PublicKey:
    return Ed25519PublicKey.from_public_bytes(bytes.fromhex(pub_hex))


def sign_vote(private_key: Ed25519PrivateKey, epoch: int, block_hash: str) -> str:
    sig = private_key.sign(vote_message(epoch, block_hash))
    return sig.hex()


def verify_vote(pub_hex: str, epoch: int, block_hash: str, sig_hex: str) -> bool:
    """验证签名。密钥/签名编码非法或验证失败一律返回 False（不抛异常）。"""
    try:
        key = _load_public_key(pub_hex)
        key.verify(bytes.fromhex(sig_hex), vote_message(epoch, block_hash))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False
