// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @title  MerkleProof
/// @notice 自包含的 Merkle 证明校验（等价于 OpenZeppelin v5 MerkleProof.verify：
///         每层做 sorted pair keccak256）。叶子在合约外已完成 abi.encodePacked + keccak256，
///         这里只负责沿证明逐层哈希，与后端 pycryptodome/web3 keccak 结果一致。
library MerkleProof {
    /// @notice 校验证明是否把 leaf 连接到 root
    /// @param proof  从叶子到根的兄弟节点哈希序列
    /// @param root   构造函数中登记的 Merkle 根
    /// @param leaf   keccak256(abi.encodePacked(chainid, contract, index, account, amount))
    function verify(bytes32[] memory proof, bytes32 root, bytes32 leaf) internal pure returns (bool) {
        return processProof(proof, leaf) == root;
        // 注：叶子本身已在链外 hash，库内部不再二次 hash，避免对 64 字节输入的多用途碰撞风险。
    }

    function processProof(bytes32[] memory proof, bytes32 leaf) internal pure returns (bytes32) {
        bytes32 computedHash = leaf;
        for (uint256 i = 0; i < proof.length; i++) {
            computedHash = _hashPair(computedHash, proof[i]);
        }
        return computedHash;
    }

    /// @dev 排序后再拼接哈希：使后端构造树时无需区分左右顺序。
    function _hashPair(bytes32 a, bytes32 b) private pure returns (bytes32 value) {
        /// @solidity memory-safe-assembly
        assembly {
            // 等价于 a < b ? keccak256(a||b) : keccak256(b||a)
            switch lt(a, b)
            case 0 {
                mstore(0x00, b)
                mstore(0x20, a)
            }
            default {
                mstore(0x00, a)
                mstore(0x20, b)
            }
            value := keccak256(0x00, 0x40)
        }
    }
}
