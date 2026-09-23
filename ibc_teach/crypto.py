"""密码学原语（真实执行，非模拟）。

包含：
* Ed25519 签名/验签（共识检查点的信任根）；
* SHA-256 域分离哈希；
* 稀疏 Merkle 树（Sparse Merkle Tree, SMT）：256 位键空间、叶子为
  ``SHA256(b"leaf-v1" || key || value)``、内部节点为
  ``SHA256(b"node-v1" || min(left,right) || max(left,right))``（内部节点无序化）。

约定（键索引 idx = SHA256(key) 视为 256 位整数）：
* 节点以 (prefix, depth) 定位：depth 为已消费的高位比特数，
  prefix = idx >> (256 - depth)（depth=0 即唯一的根；depth=256 为叶子，prefix=idx）；
* 空子树哈希可预计算：default(0) = sha256("empty-leaf")，
  default(h) = node(default(h-1), default(h-1))；
* 非成员证明 = “该叶子位置为默认空叶子”的普通包含证明，与成员证明共用同一条
  验证路径，任何长度、层级方向或哈希不符都拒绝（绑定性归约到 SHA-256 抗碰撞）。

参考：ICS-023 存在性/不存在性承诺证明（简化为单一 SMT 状态根）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Mapping, Optional

import nacl.exceptions
import nacl.signing

# 域分离前缀，防止叶子哈希被当作内部节点哈希重放。
LEAF_PREFIX = b"leaf-v1"
NODE_PREFIX = b"node-v1"
EMPTY_LEAF_SEED = b"empty-leaf-v1"

DEPTH = 256
HASH_SIZE = 32

# ---------- 基础哈希 ----------


def sha256(data: bytes) -> bytes:
    import hashlib

    return hashlib.sha256(data).digest()


def leaf_hash(key: bytes, value: bytes) -> bytes:
    return sha256(LEAF_PREFIX + key + value)


def node_hash(left: bytes, right: bytes) -> bytes:
    # 内部节点无序：固定按字节序排列，避免左右调换产生不同根。
    a, b = sorted((left, right))
    return sha256(NODE_PREFIX + a + b)


# ---------- 默认（空）子树哈希 ----------

_DEFAULT_LEVELS: list[bytes] = []


def default_levels() -> list[bytes]:
    """返回 default(h) 列表，h=0 为空叶子、h=256 为全空树根，长度 DEPTH+1。"""
    if not _DEFAULT_LEVELS:
        levels = [sha256(EMPTY_LEAF_SEED)]
        for _ in range(DEPTH):
            levels.append(node_hash(levels[-1], levels[-1]))
        _DEFAULT_LEVELS.extend(levels)
    return list(_DEFAULT_LEVELS)


# ---------- 稀疏 Merkle 树 ----------


def _key_index(key: bytes) -> int:
    """键 -> 256 位索引。"""
    return int.from_bytes(sha256(key), "big")


@dataclass(frozen=True)
class ProofStep:
    """沿叶子 -> 根方向的第 level 步（level = 0..255，0 表示叶子的兄弟）。

    * bit：本键索引在该高度的走向位（``(idx >> level) & 1``，0 左 1 右）；
    * sibling：兄弟子树根哈希（hex，即便等于默认哈希也显式给出，保证无歧义）。
    """

    level: int
    bit: int
    sibling: str  # hex

    def to_dict(self) -> dict:
        return {"level": self.level, "bit": self.bit, "sibling": self.sibling}

    @classmethod
    def from_dict(cls, d: Mapping[str, object]) -> "ProofStep":
        return cls(level=int(d["level"]), bit=int(d["bit"]), sibling=str(d["sibling"]))


class SparseMerkleTree:
    """内存稀疏 Merkle 树，仅保存非默认节点 {(prefix, depth): hash}。"""

    def __init__(self, items: Optional[Mapping[bytes, bytes]] = None) -> None:
        self.nodes: dict[tuple[int, int], bytes] = {}
        if items:
            for k, v in items.items():
                self.set(k, v)

    @staticmethod
    def _prefix(idx: int, depth: int) -> int:
        return idx if depth == DEPTH else idx >> (DEPTH - depth)

    def _get(self, prefix: int, depth: int) -> bytes:
        h = self.nodes.get((prefix, depth))
        if h is not None:
            return h
        return default_levels()[DEPTH - depth]  # 该深度空子树的高度 = 256-depth

    def set(self, key: bytes, value: bytes) -> None:
        idx = _key_index(key)
        self.nodes[(idx, DEPTH)] = leaf_hash(key, value)
        # 自叶子向根逐层重算。
        for depth in range(DEPTH - 1, -1, -1):
            child_prefix = self._prefix(idx, depth + 1)
            cur = self._get(child_prefix, depth + 1)
            sib = self._get(child_prefix ^ 1, depth + 1)
            self.nodes[(child_prefix >> 1, depth)] = node_hash(cur, sib)

    def root(self) -> bytes:
        return self._get(0, 0)

    def prove(self, key: bytes) -> list[ProofStep]:
        """生成包含证明；叶子不存在时返回的即非成员证明（空叶子）。"""
        idx = _key_index(key)
        steps: list[ProofStep] = []
        for level in range(DEPTH):  # 兄弟位于高度 level，位 (idx >> level) & 1
            prefix = self._prefix(idx, DEPTH - level) if level < DEPTH else 0
            # 高度为 level 的路径节点：叶子层 level=0 时 depth=256、prefix=idx。
            depth = DEPTH - level
            p = self._prefix(idx, depth)
            sib = self._get(p ^ 1, depth)
            steps.append(
                ProofStep(level=level, bit=(idx >> level) & 1, sibling=sib.hex())
            )
            _ = prefix
        return steps


def verify_proof(
    root_hex: str,
    key: bytes,
    value: Optional[bytes],
    steps: list[ProofStep],
) -> bool:
    """验证包含/非包含证明。

    * ``value`` 为 bytes：验证叶子承诺为 ``leaf_hash(key, value)``（成员）；
    * ``value is None``：验证叶子为默认空叶子（非成员）。

    严格要求恰好 DEPTH 步、层级连续 0..255、走向位与键索引一致；
    任何编码、长度、层级或哈希不符都返回 False。
    """
    try:
        root = bytes.fromhex(root_hex)
    except ValueError:
        return False
    if len(root) != HASH_SIZE:
        return False
    if len(steps) != DEPTH or [s.level for s in steps] != list(range(DEPTH)):
        return False

    idx = _key_index(key)
    cur = default_levels()[0] if value is None else leaf_hash(key, value)
    for step in steps:
        if step.bit not in (0, 1) or step.bit != ((idx >> step.level) & 1):
            return False
        try:
            sib = bytes.fromhex(step.sibling)
        except ValueError:
            return False
        if len(sib) != HASH_SIZE:
            return False
        cur = node_hash(cur, sib)  # node_hash 内部已按序排列
    return cur == root


# ---------- 检查点签名（Ed25519） ----------


def generate_signing_key() -> tuple[str, str]:
    """生成 Ed25519 密钥，返回 (verify_key_hex, signing_key_hex)。"""
    sk = nacl.signing.SigningKey.generate()
    return bytes(sk.verify_key).hex(), bytes(sk).hex()


def canonical_checkpoint_payload(
    chain_id: str,
    height: int,
    time_ns: int,
    app_hash: str,
    previous_app_hash: str,
) -> bytes:
    """检查点的规范化待签名负载。

    采用 ``sort_keys`` 的紧凑 JSON（键集合固定、无多余空白、以 LF 结尾）；
    链 ID、高度、时间、状态根、前一块根任一被改动都会导致签名失效。
    """
    payload = {
        "chain_id": chain_id,
        "height": int(height),
        "time_ns": int(time_ns),
        "app_hash": app_hash,
        "previous_app_hash": previous_app_hash,
    }
    return (
        json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        + "\n"
    ).encode("ascii")


def sign_checkpoint(
    signing_key_hex: str,
    chain_id: str,
    height: int,
    time_ns: int,
    app_hash: str,
    previous_app_hash: str,
) -> str:
    sk = nacl.signing.SigningKey(bytes.fromhex(signing_key_hex))
    msg = canonical_checkpoint_payload(
        chain_id, height, time_ns, app_hash, previous_app_hash
    )
    return sk.sign(msg).signature.hex()


def verify_checkpoint_signature(
    verify_key_hex: str,
    chain_id: str,
    height: int,
    time_ns: int,
    app_hash: str,
    previous_app_hash: str,
    signature_hex: str,
) -> bool:
    """验签。任何编码/长度/签名错误都返回 False（不向调用方抛异常）。"""
    try:
        vk = nacl.signing.VerifyKey(bytes.fromhex(verify_key_hex))
        sig = bytes.fromhex(signature_hex)
        msg = canonical_checkpoint_payload(
            chain_id, height, time_ns, app_hash, previous_app_hash
        )
        vk.verify(msg, sig)
        return True
    except (nacl.exceptions.BadSignatureError, ValueError):
        return False
