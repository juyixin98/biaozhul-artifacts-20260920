"""追加式（append-only）日志存储与对外操作入口。

存储布局（``data_dir``，默认 ``./log_data``）::

    leaves.jsonl                  叶子记录，每行一条 JSON，只追加
    test_ed25519_private.pem      本地测试私钥（未加密 PKCS8）
    test_ed25519_public.pem       对应公钥

本地文件防篡改
--------------
``leaves.jsonl`` 每条记录带一条 SHA-256 哈希链：
``record_hash = SHA256(prev_hash || u64 added_ms || u32 len || 叶子数据)``。
加载时从头重放整条链即可发现行被改、插、删、乱序（本地完整性检查）。
注意：这只是「本地文件」的完整性措施；日志本身的透明性保证来自
Merkle 树 + 包含/一致性证明，由外部监控者拿着旧 STH 来检验。

透明性明确不解决的问题（见 README「威胁模型」）
------------------------------------------------
单个日志运营者完全可以用不同的叶子序列构造**另一棵树**，分别向不同
观察者出示不同的 STH（日志分叉 / 伪装攻击，forking/equivocation）。
一致性证明能证明「同一个观察者先后看到的两个 STH 前缀一致」，
但无法阻止运营者对互不交换信息的观察者共谋撒谎。
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
import threading
import time
from base64 import b64decode, b64encode

from cryptography.hazmat.primitives import serialization

from . import keys as key_module
from . import merkle
from .hashing import EMPTY_TREE_HASH, leaf_hash


class CorruptLogError(RuntimeError):
    """leaves.jsonl 哈希链校验失败：记录被修改、插入、删除或乱序。"""


class Log:
    """只增透明日志。

    一个 Log 实例对应磁盘上的一个数据目录；进程内用锁保证并发追加安全。
    根哈希采用惰性计算（树较小，O(n) 重算足够；README 中如实注明）。
    """

    def __init__(self, data_dir: str = "log_data", *, auto_generate_key: bool = True):
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self._lock = threading.RLock()
        self._leaves: list[bytes] = []
        self._tail_hash = b"\x00" * 32  # 哈希链尾值；空文件时为全零哨兵
        self._records_path = os.path.join(data_dir, "leaves.jsonl")

        priv_path = os.path.join(data_dir, key_module.PRIVATE_KEY_FILENAME)
        pub_path = os.path.join(data_dir, key_module.PUBLIC_KEY_FILENAME)
        if os.path.exists(priv_path):
            self._private_key = key_module.load_private_key(priv_path)
        else:
            if not auto_generate_key:
                raise FileNotFoundError(f"测试私钥不存在：{priv_path}")
            self._private_key = key_module.generate_test_keypair()
            key_module.save_private_key(self._private_key, priv_path)
            key_module.save_public_key(self._private_key.public_key(), pub_path)

        self._load_records()

    # ------------------------------------------------------------------ 存储

    @staticmethod
    def _record_hash(prev_hash: bytes, added_ms: int, data: bytes) -> bytes:
        """计算记录哈希（定长编码，防止字段拼接歧义）。"""
        return hashlib.sha256(
            prev_hash
            + struct.pack(">Q", added_ms)
            + struct.pack(">I", len(data))
            + data
        ).digest()

    def _load_records(self) -> None:
        """重放 leaves.jsonl 并校验哈希链。"""
        self._leaves = []
        self._tail_hash = b"\x00" * 32
        if not os.path.exists(self._records_path):
            return
        prev_hash = b"\x00" * 32
        with open(self._records_path, "r", encoding="utf-8") as f:
            for lineno, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    data = b64decode(rec["leaf_b64"], validate=True)
                    added_ms = int(rec["added_ms"])
                    expected_prev = rec["prev_hash_hex"]
                    expected_hash = rec["record_hash_hex"]
                except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                    raise CorruptLogError(
                        f"第 {lineno} 行记录格式损坏：{exc}"
                    ) from exc

                if prev_hash.hex() != expected_prev:
                    raise CorruptLogError(
                        f"第 {lineno} 行前驱哈希不匹配（记录被插入/重排/删除）"
                    )
                record_hash = self._record_hash(prev_hash, added_ms, data)
                if record_hash.hex() != expected_hash:
                    raise CorruptLogError(
                        f"第 {lineno} 行记录哈希不匹配（叶子内容被篡改）"
                    )
                prev_hash = record_hash
                self._leaves.append(data)
        self._tail_hash = prev_hash

    # ------------------------------------------------------------------ 追加

    def append(self, data: bytes, *, added_ms: int | None = None) -> int:
        """追加一条叶子（字节串），返回新叶子下标（首条为 0）。

        追加即写盘并 fsync，保证只增文件顺序落盘。
        """
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("叶子数据必须是字节串 bytes")
        data = bytes(data)
        if added_ms is None:
            added_ms = time.time_ns() // 1_000_000

        with self._lock:
            record_hash = self._record_hash(self._tail_hash, added_ms, data)
            rec = {
                "leaf_b64": b64encode(data).decode("ascii"),
                "added_ms": added_ms,
                "prev_hash_hex": self._tail_hash.hex(),
                "record_hash_hex": record_hash.hex(),
            }
            with open(self._records_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
                f.flush()
                os.fsync(f.fileno())
            self._leaves.append(data)
            self._tail_hash = record_hash
            return len(self._leaves) - 1

    # ------------------------------------------------------------------ 查询

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._leaves)

    def get_leaf(self, index: int) -> bytes:
        with self._lock:
            return self._leaves[index]

    def root(self, tree_size: int | None = None) -> bytes:
        """返回前 tree_size 个叶子的树根（默认当前树）。空树返回空树根。"""
        with self._lock:
            if tree_size is None:
                tree_size = len(self._leaves)
            if tree_size > len(self._leaves):
                raise IndexError("请求的树大小超过当前日志长度")
            return merkle.tree_root(self._leaves[:tree_size])

    # ------------------------------------------------------------------ 证明

    def inclusion_proof(self, leaf_index: int, tree_size: int | None = None):
        """生成包含证明，返回 ``(叶子哈希, 证明路径, 树根, 树大小)``。"""
        with self._lock:
            if tree_size is None:
                tree_size = len(self._leaves)
            if not (0 <= tree_size <= len(self._leaves)):
                raise IndexError("树大小越界")
            if not (0 <= leaf_index < tree_size):
                raise IndexError(
                    f"叶子下标 {leaf_index} 不在大小为 {tree_size} 的树中"
                )
            leaves = self._leaves[:tree_size]
            proof = merkle.inclusion_proof(leaf_index, leaves)
            return leaf_hash(leaves[leaf_index]), proof, merkle.tree_root(leaves), tree_size

    def consistency_proof(self, first_size: int):
        """生成从 first_size 到当前大小的一致性证明。

        返回 ``(旧树根, 新树根, 证明路径, 旧大小, 新大小)``。
        first_size == 当前大小时路径为空（同一棵树，根相同即一致）。
        """
        with self._lock:
            n = len(self._leaves)
            if first_size == 0:
                raise ValueError("一致性证明要求旧树大小 > 0")
            if first_size > n:
                raise IndexError("旧树大小超过当前日志长度")
            old_root = merkle.tree_root(self._leaves[:first_size])
            new_root = merkle.tree_root(self._leaves)
            if first_size == n:
                return old_root, new_root, [], first_size, n
            proof = merkle.consistency_proof(first_size, self._leaves)
            return old_root, new_root, proof, first_size, n

    # ------------------------------------------------------------------ STH

    @property
    def public_key(self):
        return self._private_key.public_key()

    def get_sth(self, *, timestamp_ms: int | None = None) -> dict:
        """返回签名树头（Signed Tree Head）。

        字段：``tree_size``、``timestamp_ms``、``sha256_root_hash``(hex)、
        ``tree_head_signature``(hex)、``public_key_hex``（原始 32 字节）、
        ``public_key_b64``（SPKI PEM 的 base64）。
        """
        with self._lock:
            n = len(self._leaves)
            if timestamp_ms is None:
                timestamp_ms = time.time_ns() // 1_000_000
            root = merkle.tree_root(self._leaves)
            signature = key_module.sign_sth(
                self._private_key, n, timestamp_ms, root
            )
        spki_pem = self.public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        return {
            "tree_size": n,
            "timestamp_ms": timestamp_ms,
            "sha256_root_hash": root.hex(),
            "tree_head_signature": signature.hex(),
            "public_key_hex": key_module.public_key_raw(self.public_key).hex(),
            "public_key_b64": b64encode(spki_pem).decode("ascii"),
            "empty_tree_hash": EMPTY_TREE_HASH.hex(),
        }

    def verify_sth(self, sth: dict) -> bool:
        """用本日志公钥校验一个 STH 元组的签名。"""
        try:
            return key_module.verify_sth_signature(
                self.public_key,
                int(sth["tree_size"]),
                int(sth["timestamp_ms"]),
                bytes.fromhex(sth["sha256_root_hash"]),
                bytes.fromhex(sth["tree_head_signature"]),
            )
        except (KeyError, ValueError, TypeError):
            return False
