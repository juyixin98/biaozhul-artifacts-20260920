"""Merkle 树与叶子构造 —— 与 Solidity 合约严格一致。

叶子（abi.encodePacked，注意 packed 模式下 address 只占 20 字节，总长 148）：

    leaf = keccak256(
        chainid      : bytes32,  # 32 字节
        contract     : bytes32,  # 32 字节（这里显式转成 bytes32，不是 address！）
        index        : uint256,  # 32 字节
        account      : address,  # 20 字节（packed 压缩，非 32）
        amount       : uint256,  # 32 字节
    )
                                                       总计 = 32*4 + 20 = 148 字节

内部节点：sorted-pair keccak256（与 src/MerkleProof.sol 一致），
奇数层复制最后一个节点（与 test/TestMerkle.sol 一致）。
"""

from __future__ import annotations

from eth_utils import keccak

# 注意第 4 个字段是 address：eth_abi 的 packed_encode 与 Solidity 一样把它编码为 20 字节。
_LEAF_TYPES = ["bytes32", "bytes32", "uint256", "address", "uint256"]


def make_leaf(
    chain_id: int,
    contract_address: str,
    index: int,
    account: str,
    amount: int,
) -> bytes:
    """构造与链上一致的叶子哈希（148 字节 packed 输入的 keccak256）。"""
    # 手工拼接比 eth_abi.encode 更明确地表达 packed 布局（encode 对 address 是 32 字节 ABI 编码，
    # 与 packed 的 20 字节不同，故这里不使用 eth_abi.encode）。
    packed = (
        chain_id.to_bytes(32, "big")
        + int(contract_address, 16).to_bytes(32, "big")
        + index.to_bytes(32, "big")
        + int(account, 16).to_bytes(20, "big")  # packed address = 20 字节
        + amount.to_bytes(32, "big")
    )
    assert len(packed) == 148
    return keccak(packed)


def hash_pair(a: bytes, b: bytes) -> bytes:
    """排序后拼接再哈希；调用方无需知道左右关系。"""
    return keccak(a + b) if a < b else keccak(b + a)


def build_layers(leaves: list[bytes]) -> list[list[bytes]]:
    """返回从叶层到根层的所有层。奇数层复制最后一个节点。"""
    if not leaves:
        return []
    layers: list[list[bytes]] = [list(leaves)]
    while len(layers[-1]) > 1:
        layer = layers[-1]
        if len(layer) % 2 == 1:
            layer = layer + [layer[-1]]
        layers.append([hash_pair(layer[i], layer[i + 1]) for i in range(0, len(layer), 2)])
    return layers


def get_root(leaves: list[bytes]) -> bytes:
    layers = build_layers(leaves)
    if not layers:
        raise ValueError("cannot build a tree from zero leaves")
    return layers[-1][0]


def get_proof(leaves: list[bytes], index: int) -> list[bytes]:
    """生成从叶子到根、每层一个兄弟节点的证明。

    奇数层最后一个节点的兄弟取它自身（对应建树时的"复制"规则）；
    合约侧 sorted-pair 哈希 hashPair(x, x) 与此自洽。
    """
    layers = build_layers(leaves)
    proof: list[bytes] = []
    pos = index
    for layer in layers[:-1]:
        if pos % 2 == 0:
            sibling_pos = pos + 1
            proof.append(layer[sibling_pos] if sibling_pos < len(layer) else layer[pos])
        else:
            proof.append(layer[pos - 1])
        pos //= 2
    return proof


def verify_proof(proof: list[bytes], root: bytes, leaf: bytes) -> bool:
    """纯 Python 端的校验（与链上 MerkleProof.verify 同一算法），用于自检/单测。"""
    node = leaf
    for sibling in proof:
        node = hash_pair(node, sibling)
    return node == root
