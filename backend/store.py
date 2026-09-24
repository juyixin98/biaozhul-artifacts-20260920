"""内存中的分配表 + Merkle 树存储（演示级，单进程）。"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Dict, List, Optional

from web3 import Web3

from .merkle import MerkleTree, build_tree_from_allocations


class AllocationStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._allocations: Dict[int, dict] = {}
        self._tree: Optional[MerkleTree] = None
        self._chain_id: Optional[int] = None
        self._contract: Optional[str] = None

    # ---------------- 装载 ----------------

    def load(self, allocations: List[dict], chain_id: int, contract_address: str) -> bytes:
        """用分配表重建树（叶子绑定 chain_id 与 contract_address），返回 root。"""
        contract_address = Web3.to_checksum_address(contract_address)
        tree, norm = build_tree_from_allocations(allocations, chain_id, contract_address)
        with self._lock:
            self._allocations = {a["index"]: a for a in norm}
            self._tree = tree
            self._chain_id = chain_id
            self._contract = contract_address
        return tree.root

    def load_file(self, path: str | Path, chain_id: int, contract_address: str) -> bytes:
        data = json.loads(Path(path).read_text())
        if isinstance(data, dict) and "allocations" in data:
            data = data["allocations"]
        if not isinstance(data, list):
            raise ValueError("分配表格式：[{index, account, amount}, ...]")
        return self.load(data, chain_id, contract_address)

    # ---------------- 查询 ----------------

    @property
    def chain_id(self) -> int:
        with self._lock:
            if self._chain_id is None:
                raise RuntimeError("分配表尚未装载")
            return self._chain_id

    @property
    def contract(self) -> str:
        with self._lock:
            if self._contract is None:
                raise RuntimeError("分配表尚未装载")
            return self._contract

    @property
    def root(self) -> bytes:
        with self._lock:
            if self._tree is None:
                raise RuntimeError("分配表尚未装载")
            return self._tree.root

    def count(self) -> int:
        with self._lock:
            return len(self._allocations)

    def list_allocations(self) -> List[dict]:
        with self._lock:
            return [self._allocations[i] for i in sorted(self._allocations)]

    def get(self, index: int) -> dict:
        with self._lock:
            if index not in self._allocations:
                raise KeyError(index)
            return dict(self._allocations[index])

    def proof_for_index(self, index: int) -> dict:
        """返回单项的完整证明信息。"""
        from .merkle import encode_leaf

        with self._lock:
            if self._tree is None:
                raise RuntimeError("分配表尚未装载")
            a = self.get(index)
            leaf = encode_leaf(self._chain_id, self._contract, a["index"],
                               a["account"], a["amount"])
            proof = self._tree.proof(leaf)
            # 自检：证明必须能验证通过
            assert MerkleTree.verify(leaf, proof, self._tree.root), "内部证明自检失败"
        return {
            "index": a["index"],
            "account": a["account"],
            "amount": a["amount"],
            "leaf": "0x" + leaf.hex(),
            "proof": ["0x" + p.hex() for p in proof],
        }

    def proofs_for_indices(self, indices: List[int]) -> List[dict]:
        """批量取证明。indices 必须唯一（链上同批次重复索引会回滚）。"""
        if len(set(indices)) != len(indices):
            raise ValueError("批次内索引必须唯一")
        return [self.proof_for_index(i) for i in indices]


store = AllocationStore()
