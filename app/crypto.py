"""密码学与规范化编码。

- 签名：真实 Ed25519（cryptography 库），不是模拟。
- 规范化：固定字段顺序 + 定长前缀，避免 JSON 空格/键序歧义；
  签名覆盖的字节同时也是 content_hash / 证据 ID 的计算基础。
- 跨链隔离：chain_id 参与签名，同一张票换个链提交会因链注册表校验失败而被拒绝，
  且签名本身也无法在别的链上重放为「有效签名的不同内容」。
"""
from __future__ import annotations

import hashlib
import struct
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from .config import DOMAIN_SEPARATOR


def _u64(n: int) -> bytes:
    if n < 0 or n > 0xFFFFFFFFFFFFFFFF:
        raise ValueError("unsigned 64-bit integer out of range")
    return struct.pack(">Q", n)


def _b(b: bytes) -> bytes:
    return _u64(len(b)) + b


def vote_signed_bytes(
    *,
    chain_id: str,
    validator_pubkey: bytes,
    round: int,
    block_hash: bytes,
) -> bytes:
    """一张离线投票「被签名覆盖」的规范字节。

    布局：DOMAIN | len(chain_id) | chain_id | len(pubkey) | pubkey
          | u64(round) | len(block_hash) | block_hash
    """
    chain = chain_id.encode("utf-8")
    return b"".join(
        [
            DOMAIN_SEPARATOR,
            _b(chain),
            _b(validator_pubkey),
            _u64(round),
            _b(block_hash),
        ]
    )


def sign_vote(
    private_key: Ed25519PrivateKey,
    *,
    chain_id: str,
    validator_pubkey: bytes,
    round: int,
    block_hash: bytes,
) -> bytes:
    return private_key.sign(
        vote_signed_bytes(
            chain_id=chain_id,
            validator_pubkey=validator_pubkey,
            round=round,
            block_hash=block_hash,
        )
    )


def verify_signature(public_key: bytes, signature: bytes, message: bytes) -> bool:
    """真实 Ed25519 验签。任何异常（坏公钥/坏签名长度）一律视为验签失败。"""
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, message)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def sha256(data: bytes) -> bytes:
    """原始 32 字节摘要；对外展示一律用 .hex()。"""
    return hashlib.sha256(data).digest()


def content_hash(
    *,
    chain_id: str,
    validator_pubkey: bytes,
    round: int,
    block_hash: bytes,
) -> bytes:
    """对签名覆盖字节取 SHA-256，作为「相同投票」的判定依据。"""
    return sha256(
        vote_signed_bytes(
            chain_id=chain_id,
            validator_pubkey=validator_pubkey,
            round=round,
            block_hash=block_hash,
        )
    )


def canonical_evidence_blob(vote_contents: list[bytes]) -> bytes:
    """证据规范化：对两份冲突票的「签名覆盖字节」按字典序排序后拼接，
    再前置数量。逆序到达得到完全相同的字节 -> 相同证据 ID。"""
    ordered = sorted(vote_contents)
    parts = [b"EVIDENCE/v1:", _u64(len(ordered))]
    for item in ordered:
        parts.append(_b(item))
    return b"".join(parts)


def evidence_id(vote_contents: list[bytes]) -> bytes:
    return sha256(canonical_evidence_blob(vote_contents))


def snapshot_canonical(entries: list[tuple[bytes, int]]) -> bytes:
    """权益快照规范字节：按公钥排序，条目 = len(pubkey)|pubkey|u64(power)。"""
    parts = [b"SNAPSHOT/v1:", _u64(len(entries))]
    for pubkey, power in sorted(entries):
        parts.append(_b(pubkey))
        parts.append(_u64(power))
    return b"".join(parts)


def bytes_to_hex(b: bytes | None) -> str | None:
    return b.hex() if b is not None else None


def hex_to_bytes(h: str, name: str) -> bytes:
    try:
        out = bytes.fromhex(h)
    except ValueError as exc:
        raise ValueError(f"{name} 必须是十六进制字符串") from exc
    if not out:
        raise ValueError(f"{name} 不能为空")
    return out


def derive_pubkey(private_key: Ed25519PrivateKey) -> bytes:
    return private_key.public_key().public_bytes_raw()
