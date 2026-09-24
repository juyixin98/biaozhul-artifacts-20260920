"""Merkle 模块单元测试（不依赖链）。"""
from __future__ import annotations

import pytest
from eth_abi.packed import encode_packed
from eth_utils import keccak

from backend.merkle import (
    MerkleTree,
    build_tree_from_allocations,
    encode_leaf,
    hash_pair,
)

CHAIN = 31337
CONTRACT = "0x1111111111111111111111111111111111111111"
A1 = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
A2 = "0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC"
A3 = "0x90F79bf6EB2c4f870365E785982E1f101E93b906"


def _oracle_leaf(chain_id, addr, idx, acct, amt):
    """用 eth_abi.encode_packed 作为独立参照实现。"""
    packed = encode_packed(
        ["uint256", "address", "uint256", "address", "uint256"],
        [chain_id, addr, idx, acct, amt],
    )
    return keccak(packed)


def test_encode_leaf_matches_abi_encode_packed():
    got = encode_leaf(CHAIN, CONTRACT, 7, A1, 123456789)
    want = _oracle_leaf(CHAIN, CONTRACT, 7, A1, 123456789)
    assert got == want
    assert len(got) == 32


@pytest.mark.parametrize("kw", [
    dict(chain_id=1), dict(contract=A1), dict(index=8), dict(account=A2), dict(amount=1),
])
def test_encode_leaf_binds_every_field(kw):
    base = dict(chain_id=CHAIN, contract_address=CONTRACT, index=7, account=A1, amount=123)
    changed = dict(base)
    mapping = dict(chain_id="chain_id", contract="contract_address", index="index",
                   account="account", amount="amount")
    key = next(iter(kw))
    changed[mapping[key]] = kw[key]
    assert encode_leaf(**changed) != encode_leaf(**base)


def test_single_leaf_tree_root_is_leaf():
    leaf = encode_leaf(CHAIN, CONTRACT, 0, A1, 1)
    t = MerkleTree([leaf])
    assert t.root == leaf
    assert t.proof(leaf) == []
    assert MerkleTree.verify(leaf, [], t.root)


def test_proofs_verify_for_all_leaves_various_sizes():
    for n in range(1, 9):
        leaves = [encode_leaf(CHAIN, CONTRACT, i, A1 if i % 2 else A2, 1000 + i)
                  for i in range(n)]
        t = MerkleTree(leaves)
        for i, leaf in enumerate(leaves):
            p = t.proof(leaf)
            assert MerkleTree.verify(leaf, p, t.root), f"n={n} i={i}"
            # 证明深度：ceil(log2(n))（奇数位复制也算一层）
            assert len(p) == (n - 1).bit_length()


def test_wrong_proof_fails():
    leaves = [encode_leaf(CHAIN, CONTRACT, i, A1, i + 1) for i in range(4)]
    t = MerkleTree(leaves)
    p0 = t.proof(leaves[0])
    # 用叶子 1 的证明伪造叶子 0
    p1 = t.proof(leaves[1])
    assert not MerkleTree.verify(leaves[0], p1, t.root)
    # 篡改一个兄弟节点
    p0_bad = list(p0)
    p0_bad[0] = bytes(32)
    assert not MerkleTree.verify(leaves[0], p0_bad, t.root)
    # 换根也失败
    assert not MerkleTree.verify(leaves[0], p0, bytes(32))


def test_ordered_pair_is_commutative_ordering_insensitive():
    a = encode_leaf(CHAIN, CONTRACT, 0, A1, 1)
    b = encode_leaf(CHAIN, CONTRACT, 1, A2, 2)
    assert hash_pair(a, b) == hash_pair(b, a)


def test_build_from_allocations_sorted_and_bound():
    allocs = [
        {"index": 2, "account": A3, "amount": 30},
        {"index": 0, "account": A1, "amount": 10},
        {"index": 1, "account": A2, "amount": 20},
    ]
    t, norm = build_tree_from_allocations(allocs, CHAIN, CONTRACT)
    assert [a["index"] for a in norm] == [0, 1, 2]
    # 叶子确实按 chainId/contract 绑定
    l0 = encode_leaf(CHAIN, CONTRACT, 0, A1, 10)
    assert t.leaves[0] == l0
    t2, _ = build_tree_from_allocations(allocs, CHAIN + 1, CONTRACT)
    assert t2.root != t.root
    t3, _ = build_tree_from_allocations(allocs, CHAIN, A2)
    assert t3.root != t.root


def test_duplicate_index_rejected():
    with pytest.raises(ValueError):
        build_tree_from_allocations(
            [{"index": 0, "account": A1, "amount": 1},
             {"index": 0, "account": A2, "amount": 2}], CHAIN, CONTRACT)


def test_duplicate_leaf_rejected_same_salt_collision():
    # 同 index 同账户同金额会产生相同叶子（一般由 index 唯一约束先拦截）
    leaf = encode_leaf(CHAIN, CONTRACT, 0, A1, 1)
    with pytest.raises(ValueError):
        MerkleTree([leaf, leaf])
