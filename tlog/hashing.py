"""哈希原语与域分离（domain separation）。

严格遵循 RFC 9162（Certificate Transparency v2）第 2.1 节：

* 叶子哈希:   SHA256(0x00 || data)
* 内部节点:   SHA256(0x01 || left_hash || right_hash)
* 空树根:     SHA256(b"")

0x00 / 0x01 前缀就是「域分离」：攻击者无法把内部节点的哈希冒充成叶子，
也无法把单叶树的根冒充成内部节点（第二原像防护的关键）。

不自创哈希算法：仅使用标准库 hashlib 的 SHA-256（RFC 9162 默认算法）。
"""

from __future__ import annotations

import hashlib

# RFC 9162 第 2.1 节规定的叶子/内部节点域分隔字节。
LEAF_PREFIX = b"\x00"
NODE_PREFIX = b"\x01"

# SHA-256 输出长度（字节）。所有树节点哈希均为 32 字节。
HASH_SIZE = 32

# 空树的根 = HASH()（RFC 9162：MTH({}) = HASH()）
EMPTY_TREE_HASH = hashlib.sha256(b"").digest()


def leaf_hash(data: bytes) -> bytes:
    """计算叶子哈希 HASH(0x00 || data)。"""
    return hashlib.sha256(LEAF_PREFIX + data).digest()


def node_hash(left: bytes, right: bytes) -> bytes:
    """计算内部节点哈希 HASH(0x01 || left || right)。"""
    if len(left) != HASH_SIZE or len(right) != HASH_SIZE:
        raise ValueError("内部节点的两个子节点都必须是 32 字节哈希")
    return hashlib.sha256(NODE_PREFIX + left + right).digest()


def hash_empty() -> bytes:
    """空树根（常量，便于引用）。"""
    return EMPTY_TREE_HASH
