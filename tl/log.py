"""High-level transparent log service: append, STH, inclusion, consistency."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import List, Optional

from . import merkle
from .signing import KeyManager, SignedTreeHead
from .store import LogStore


@dataclass
class InclusionResult:
    leaf_index: int
    tree_size: int
    leaf_hash: bytes
    proof: List[bytes]
    root_hash: bytes


@dataclass
class ConsistencyResult:
    old_size: int
    new_size: int
    proof: List[bytes]
    old_root: bytes
    new_root: bytes


class TransparentLog:
    """Append-only Merkle log with signed tree heads."""

    def __init__(self, store: LogStore, keys: KeyManager):
        self.store = store
        self.keys = keys
        self._lock = threading.RLock()

    def add(self, data: bytes) -> int:
        with self._lock:
            return self.store.append(data)

    def size(self) -> int:
        return self.store.size

    def current_sth(self) -> SignedTreeHead:
        """Sign the current tree head (fresh timestamp on each call)."""
        with self._lock:
            size = self.store.size
            root = self.store.root_at(size)
            ts = time.time_ns() // 1000  # microseconds
            return self.keys.sign_sth(size, root, ts)

    def sth_at_size(self, tree_size: int) -> SignedTreeHead:
        """Sign a head for an explicit (historical) size; must be <= current."""
        with self._lock:
            if not (0 <= tree_size <= self.store.size):
                raise IndexError(
                    f"tree_size {tree_size} out of range [0,{self.store.size}]"
                )
            root = self.store.root_at(tree_size)
            ts = time.time_ns() // 1000
            return self.keys.sign_sth(tree_size, root, ts)

    def entry(self, index: int):
        return self.store.entry(index)

    def inclusion_by_index(
        self, leaf_index: int, tree_size: Optional[int] = None
    ) -> InclusionResult:
        """Build an inclusion proof for an existing leaf.

        ``tree_size`` defaults to the current tree size.  It must satisfy
        leaf_index < tree_size <= current size so the client can audit a
        specific head.
        """
        with self._lock:
            current = self.store.size
            if tree_size is None:
                tree_size = current
            if not (0 <= leaf_index < tree_size <= current):
                raise IndexError(
                    f"require 0 <= index ({leaf_index}) < tree_size "
                    f"({tree_size}) <= current ({current})"
                )
            leaves = self.store.leaf_hashes()[:tree_size]
            proof = merkle.inclusion_proof_hashes(leaf_index, leaves)
            root = merkle.tree_hash(leaves)
            return InclusionResult(
                leaf_index=leaf_index,
                tree_size=tree_size,
                leaf_hash=leaves[leaf_index],
                proof=proof,
                root_hash=root,
            )

    def consistency(
        self, old_size: int, new_size: Optional[int] = None
    ) -> ConsistencyResult:
        with self._lock:
            current = self.store.size
            if new_size is None:
                new_size = current
            if not (0 <= old_size <= new_size <= current):
                raise IndexError(
                    f"require 0 <= old ({old_size}) <= new ({new_size}) "
                    f"<= current ({current})"
                )
            leaves = self.store.leaf_hashes()
            proof = merkle.consistency_proof_hashes(old_size, leaves[:new_size])
            return ConsistencyResult(
                old_size=old_size,
                new_size=new_size,
                proof=proof,
                old_root=merkle.tree_hash(leaves[:old_size]),
                new_root=merkle.tree_hash(leaves[:new_size]),
            )

    def roots_for_sizes(self, old_size: int, new_size: int):
        return self.store.root_at(old_size), self.store.root_at(new_size)
