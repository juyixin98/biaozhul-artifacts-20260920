"""Merkle 树与叶子编码 —— 必须与链上合约逐字节一致。

链上叶子（src/MerkleClaim.sol）：
    keccak256(abi.encodePacked(uint256 chainId, address contract, uint256 index,
                               address account, uint256 amount))
即：32 | 20 | 32 | 20 | 32 字节的定长拼接后 keccak。

树规则（src/MerkleProof.sol 与 test/helpers/TreeBuilder.sol 一致）：
    pair(a, b) = keccak(min(a,b) || max(a,b))   —— 有序配对
    奇数层最后一个节点与自身配对（复制提升）
    只有一个叶子时：root = leaf，proof = []
"""
from __future__ import annotations

from typing import Iterable, List, Mapping, Sequence

from eth_utils import keccak
from web3 import Web3

# 各字段 packed 后的字节宽度（uint256=32, address=20）
_PACKED_LAYOUT = (32, 20, 32, 20, 32)


def _addr(x: str) -> bytes:
    raw = bytes.fromhex(Web3.to_checksum_address(x)[2:])
    if len(raw) != 20:
        raise ValueError(f"bad address: {x}")
    return raw


def _u256(x: int) -> bytes:
    x = int(x)
    if x < 0 or x >= 2**256:
        raise ValueError("uint256 out of range")
    return x.to_bytes(32, "big")


def encode_leaf(chain_id: int, contract_address: str, index: int,
                account: str, amount: int) -> bytes:
    """与合约 leafHash 完全相同的叶子哈希。"""
    blob = (
        _u256(chain_id)
        + _addr(contract_address)
        + _u256(index)
        + _addr(account)
        + _u256(amount)
    )
    assert len(blob) == sum(_PACKED_LAYOUT) == 136
    return keccak(blob)


def hash_pair(a: bytes, b: bytes) -> bytes:
    """有序配对：小的在左。"""
    return keccak(min(a, b) + max(a, b))


class MerkleTree:
    """按给定叶子顺序构造的二叉 Merkle 树（不重排叶子）。"""

    def __init__(self, leaves: Sequence[bytes]):
        if not leaves:
            raise ValueError("cannot build tree from empty leaves")
        if len(set(leaves)) != len(leaves):
            raise ValueError("duplicate leaf in tree (index/account/amount 重复?)")
        self._leaves: List[bytes] = list(leaves)
        self._layers: List[List[bytes]] = [self._leaves]
        cur = self._leaves
        while len(cur) > 1:
            nxt: List[bytes] = []
            for i in range(0, len(cur), 2):
                a = cur[i]
                b = cur[i + 1] if i + 1 < len(cur) else a  # 奇数位复制
                nxt.append(hash_pair(a, b))
            self._layers.append(nxt)
            cur = nxt

    @property
    def root(self) -> bytes:
        return self._layers[-1][0]

    @property
    def leaves(self) -> List[bytes]:
        return list(self._leaves)

    def proof(self, leaf: bytes) -> List[bytes]:
        """返回 leaf → root 路径上的兄弟节点序列。"""
        pos = self._layers[0].index(leaf)  # 不存在则 ValueError
        out: List[bytes] = []
        for level in self._layers[:-1]:
            sib_pos = pos ^ 1
            if sib_pos >= len(level):
                out.append(level[pos])  # 提升节点：兄弟即自身
            else:
                out.append(level[sib_pos])
            pos >>= 1
        return out

    @staticmethod
    def verify(leaf: bytes, proof: Iterable[bytes], root: bytes) -> bool:
        """独立校验（与合约 MerkleProof.verify 同算法），供测试/自检使用。"""
        h = leaf
        for sib in proof:
            h = hash_pair(h, sib)
        return h == root


def build_tree_from_allocations(
    allocations: Sequence[Mapping[str, object]],
    chain_id: int,
    contract_address: str,
) -> tuple[MerkleTree, List[dict]]:
    """从分配表构造树。

    allocations: [{"index": int, "account": "0x..", "amount": int(wei)}, ...]
    必须包含每个 allocation 的 index，且 index 唯一。
    返回 (tree, normalized_allocations)，normalized 按 index 升序、字段标准化。
    """
    norm = sorted(
        (
            {
                "index": int(a["index"]),
                "account": Web3.to_checksum_address(str(a["account"])),
                "amount": int(a["amount"]),
            }
            for a in allocations
        ),
        key=lambda x: x["index"],
    )
    if len({a["index"] for a in norm}) != len(norm):
        raise ValueError("duplicate index in allocations")
    contract_address = Web3.to_checksum_address(contract_address)
    leaves = [
        encode_leaf(chain_id, contract_address, a["index"], a["account"], a["amount"])
        for a in norm
    ]
    return MerkleTree(leaves), norm
