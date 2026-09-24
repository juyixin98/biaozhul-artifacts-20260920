// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

/// @title  MerkleProof
/// @notice 极简 Merkle 证明校验（有序配对哈希），无外部依赖。
///         叶子与中间节点均为 32 字节；proof 为叶子→根路径上的兄弟节点序列。
library MerkleProof {
    /// @dev 校验单个叶子：processProof(proof, leaf) == root
    function verify(bytes32[] memory proof, bytes32 root, bytes32 leaf) internal pure returns (bool) {
        return processProof(proof, leaf) == root;
    }

    function processProof(bytes32[] memory proof, bytes32 leaf) internal pure returns (bytes32 h) {
        h = leaf;
        for (uint256 i = 0; i < proof.length; i++) {
            h = hashPair(h, proof[i]);
        }
    }

    /// @dev 有序配对：较小者在左。叶子统一为 32 字节，不存在 64/32 字节第二原像歧义。
    function hashPair(bytes32 a, bytes32 b) internal pure returns (bytes32) {
        return a < b
            ? keccak256(abi.encodePacked(a, b))
            : keccak256(abi.encodePacked(b, a));
    }
}
