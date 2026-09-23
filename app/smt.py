# -*- coding: utf-8 -*-
"""256 层稀疏 Merkle 树（Sparse Merkle Trie）。

教学说明：IBC 中继器不能让对端链直接读自己的数据库，而是提交
*状态存在性 / 不存在性证明*：
- key 的 sha256 摘要决定一条 256 位路径；
- 每个内部节点 hash = sha256(0x01 || left || right)；
- 叶节点 hash = sha256(0x00 || key || value)，空值用全零 ZERO 表示“不存在”；
- 证明是路径上 256 个兄弟哈希，对端从叶子重算到根并与检查点 app_hash 比较。

不存在性证明 = 该路径叶值为 ZERO。因此“未收到包”（ordered 通道序列号
尚未推进）和“无回执/无确认”都能被密码学地证明。
"""
from __future__ import annotations

from typing import Optional, Protocol

from .crypto import ZERO, sha256

DEPTH = 256
LEAF_PREFIX = b"\x00"
INNER_PREFIX = b"\x01"


def key_path(key: bytes) -> bytes:
    """状态键 -> 256 位路径（sha256 摘要）。"""
    return sha256(key)


def leaf_hash(path: bytes, value: bytes) -> bytes:
    """叶槽哈希。path 是 key 的 sha256（256 位路径），与 verify 端一致。"""
    return sha256(LEAF_PREFIX + path + value)


def inner_hash(left: bytes, right: bytes) -> bytes:
    return sha256(INNER_PREFIX + left + right)


def _bit(path: bytes, level: int) -> int:
    return (path[level // 8] >> (7 - (level % 8))) & 1


def default_hashes() -> list[bytes]:
    """空树各层默认哈希。

    default[256] = ZERO（空叶槽），default[h] = H(default[h+1]||default[h+1])。
    """
    table = [ZERO] * (DEPTH + 1)
    for h in range(DEPTH - 1, -1, -1):
        table[h] = inner_hash(table[h + 1], table[h + 1])
    return table


DEFAULTS = default_hashes()


class NodeStore(Protocol):
    """节点与键值的存储接口（SQLite 与内存两种实现，测试中交叉验证）。"""

    def get_node(self, key: bytes) -> Optional[bytes]: ...
    def put_node(self, key: bytes, value_hash: bytes) -> None: ...
    def delete_node(self, key: bytes) -> None: ...
    def get_value(self, key: bytes) -> Optional[bytes]: ...
    def put_value(self, key: bytes, value: bytes) -> None: ...
    def delete_value(self, key: bytes) -> None: ...


class MemoryNodeStore:
    def __init__(self) -> None:
        self.nodes: dict[bytes, bytes] = {}
        self.kv: dict[bytes, bytes] = {}

    def get_node(self, key: bytes) -> Optional[bytes]:
        return self.nodes.get(key)

    def put_node(self, key: bytes, value_hash: bytes) -> None:
        self.nodes[key] = value_hash

    def delete_node(self, key: bytes) -> None:
        self.nodes.pop(key, None)

    def get_value(self, key: bytes) -> Optional[bytes]:
        return self.kv.get(key)

    def put_value(self, key: bytes, value: bytes) -> None:
        self.kv[key] = value

    def delete_value(self, key: bytes) -> None:
        self.kv.pop(key, None)


class SparseMerkleTree:
    def __init__(self, store: NodeStore):
        self.store = store

    # ---- 根 ----
    def root(self) -> bytes:
        h = self.store.get_node(b"\x00root")
        return h if h is not None else DEFAULTS[0]

    def _set_root(self, h: bytes) -> None:
        if h == DEFAULTS[0]:
            self.store.delete_node(b"\x00root")
        else:
            self.store.put_node(b"\x00root", h)

    # ---- 读 ----
    def get(self, key: bytes) -> Optional[bytes]:
        """返回存储的值；不存在（或被显式删除）返回 None。"""
        return self.store.get_value(key)

    def _node_hash(self, node_key: bytes, level: int) -> bytes:
        h = self.store.get_node(node_key)
        return h if h is not None else DEFAULTS[level]

    # ---- 写（真实自底向上更新根）----
    def set(self, key: bytes, value: bytes) -> bytes:
        path = key_path(key)
        if value:
            self.store.put_value(key, value)
        else:
            self.store.delete_value(key)

        # 自顶向下记录路径节点键
        node_keys: list[bytes] = [b"\x00root"]
        cur = b"\x00root"
        for level in range(DEPTH):
            cur = cur + bytes([path[level // 8] >> (7 - (level % 8)) & 1])
            node_keys.append(cur)

        new_leaf = leaf_hash(path, value) if value else ZERO
        child_hash = new_leaf
        leaf_store_key = node_keys[DEPTH]
        if value:
            self.store.put_node(leaf_store_key, new_leaf)
        else:
            # 叶槽回归全零：删除存储节点，读取方将回落到默认 ZERO
            self.store.delete_node(leaf_store_key)

        # 自底向上重算兄弟与父节点
        for level in range(DEPTH - 1, -1, -1):
            nk = node_keys[level]
            child_key = node_keys[level + 1]
            b = _bit(path, level)
            if b == 0:
                left = child_hash
                right = self._sibling_hash(child_key, level + 1)
            else:
                left = self._sibling_hash(child_key, level + 1)
                right = child_hash
            h = inner_hash(left, right)
            if h == DEFAULTS[level]:
                self.store.delete_node(nk)
            else:
                self.store.put_node(nk, h)
            child_hash = h
        self._set_root(child_hash)
        return child_hash

    def delete(self, key: bytes) -> bytes:
        return self.set(key, b"")

    def _sibling_hash(self, child_node_key: bytes, child_level: int) -> bytes:
        # child_node_key 最后一字节是 0/1 的路径位；翻转即兄弟
        sibling_key = child_node_key[:-1] + bytes([child_node_key[-1] ^ 1])
        return self._node_hash(sibling_key, child_level)

    # ---- 证明 ----
    def prove(self, key: bytes) -> dict:
        """生成证明；membership=False 表示该 key 当前不存在（叶值为 ZERO）。"""
        path = key_path(key)
        value = self.store.get_value(key)
        if value is None:
            value = b""
        siblings: list[bytes] = []
        cur = b"\x00root"
        for level in range(DEPTH):
            bit = _bit(path, level)
            child_key = cur + bytes([bit])
            sibling_key = cur + bytes([bit ^ 1])
            siblings.append(self._node_hash(sibling_key, level + 1))
            cur = child_key
        return {
            "key": key,
            "value": value,
            "membership": bool(value),
            "siblings": siblings,  # 自顶向下 256 个，顺序固定
        }

    # ---- 从另一个根独立验证（不依赖本地任何状态）----
    @staticmethod
    def verify(root: bytes, key: bytes, value: bytes, siblings: list[bytes]) -> bytes:
        """根据证明重算根。调用方负责比较返回值与检查点 app_hash。"""
        path = key_path(key)
        if len(siblings) != DEPTH:
            raise ValueError(f"证明必须包含 {DEPTH} 个兄弟哈希，实际 {len(siblings)}")
        h = leaf_hash(path, value) if value else ZERO
        for level in range(DEPTH - 1, -1, -1):
            sib = siblings[level]
            if len(sib) != 32:
                raise ValueError(f"第 {level} 层兄弟哈希不是 32 字节")
            if _bit(path, level) == 0:
                h = inner_hash(h, sib)
            else:
                h = inner_hash(sib, h)
        return h
