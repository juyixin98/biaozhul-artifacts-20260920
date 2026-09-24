"""不依赖链的纯逻辑测试：Python 树构造、证明、叶子域绑定。"""

from __future__ import annotations

import pytest

from app.merkle import build_layers, get_proof, get_root, hash_pair, make_leaf, verify_proof

CHAIN_ID = 31337
CONTRACT_A = "0x1111111111111111111111111111111111111111"
CONTRACT_B = "0x2222222222222222222222222222222222222222"
ACCOUNT = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"


def _leaves(n: int, contract: str = CONTRACT_A, chain_id: int = CHAIN_ID) -> list[bytes]:
    return [make_leaf(chain_id, contract, i, ACCOUNT, (i + 1) * 10**18) for i in range(n)]


@pytest.mark.parametrize("n", [1, 2, 3, 4, 5, 7, 8, 9, 15, 16, 17])
def test_proof_verifies_for_all_tree_shapes(n):
    leaves = _leaves(n)
    root = get_root(leaves)
    for i in range(n):
        proof = get_proof(leaves, i)
        assert verify_proof(proof, root, leaves[i])


def test_single_leaf_tree_root_equals_leaf():
    leaves = _leaves(1)
    assert get_root(leaves) == leaves[0]
    assert get_proof(leaves, 0) == []


def test_odd_layer_duplicates_last_node():
    # 3 个叶子：第二层为 2 个节点（h01, h2'），h2' = hash_pair(l2, l2)
    leaves = _leaves(3)
    layers = build_layers(leaves)
    assert len(layers) == 3  # 叶层(3) -> 2 -> 1
    assert layers[1][1] == hash_pair(leaves[2], leaves[2])


def test_leaf_binds_chain_id():
    a = make_leaf(CHAIN_ID, CONTRACT_A, 0, ACCOUNT, 1)
    b = make_leaf(1, CONTRACT_A, 0, ACCOUNT, 1)
    assert a != b


def test_leaf_binds_contract_address():
    a = make_leaf(CHAIN_ID, CONTRACT_A, 0, ACCOUNT, 1)
    b = make_leaf(CHAIN_ID, CONTRACT_B, 0, ACCOUNT, 1)
    assert a != b


def test_leaf_binds_index_account_amount():
    base = make_leaf(CHAIN_ID, CONTRACT_A, 0, ACCOUNT, 1)
    assert make_leaf(CHAIN_ID, CONTRACT_A, 1, ACCOUNT, 1) != base
    assert make_leaf(CHAIN_ID, CONTRACT_A, 0, CONTRACT_B, 1) != base
    assert make_leaf(CHAIN_ID, CONTRACT_A, 0, ACCOUNT, 2) != base


def test_wrong_proof_rejected():
    leaves = _leaves(4)
    root = get_root(leaves)
    # 拿 index 1 的证明去验 index 0 的叶子
    assert not verify_proof(get_proof(leaves, 1), root, leaves[0])


def test_empty_tree_rejected():
    with pytest.raises(ValueError):
        get_root([])


def test_root_is_order_dependent():
    # 叶子顺序不同，根不同（交换 index 与数量的绑定不能重放）
    l1 = _leaves(3)
    l2 = list(reversed(_leaves(3)))
    assert get_root(l1) != get_root(l2)
