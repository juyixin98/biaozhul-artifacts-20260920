// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @title  TestMerkle
/// @notice 仅测试用：在 Solidity 内构造与后端 Python 相同规则的 Merkle 树
///         （odd 层复制最后一个节点；sorted-pair keccak256），用于在链上测试中生成证明，
///         不参与生产部署。
library TestMerkle {
    function hashLeaf(
        uint256 chainId,
        address claimContract,
        uint256 index,
        address account,
        uint256 amount
    ) internal pure returns (bytes32) {
        return
            keccak256(
                abi.encodePacked(
                    bytes32(chainId),
                    bytes32(uint256(uint160(claimContract))),
                    index,
                    account,
                    amount
                )
            );
    }

    /// @dev 从叶层开始逐层向上；奇数个节点时最后一个节点与自身配对（复制）。
    function getRoot(bytes32[] memory leaves) internal pure returns (bytes32) {
        bytes32[] memory layer = leaves;
        while (layer.length > 1) {
            layer = _nextLayer(layer);
        }
        return layer.length == 1 ? layer[0] : bytes32(0);
    }

    /// @dev 保存每一层，叶子的证明由各层中 index/2 位置的兄弟组成。
    function getProof(bytes32[] memory leaves, uint256 index)
        internal
        pure
        returns (bytes32[] memory)
    {
        bytes32[] memory proof = new bytes32[](_depth(leaves.length));
        bytes32[] memory layer = leaves;
        uint256 pos = index;
        uint256 p;
        while (layer.length > 1) {
            uint256 sibling = pos % 2 == 0 ? pos + 1 : pos - 1;
            // 奇数层最后一个节点的"兄弟"是它自己（复制规则）
            proof[p++] = sibling < layer.length ? layer[sibling] : layer[pos];
            layer = _nextLayer(layer);
            pos /= 2;
        }
        return proof;
    }

    function _depth(uint256 n) private pure returns (uint256 d) {
        if (n <= 1) return 0;
        n--;
        while (n > 0) {
            n >>= 1;
            d++;
        }
    }

    function _nextLayer(bytes32[] memory layer) private pure returns (bytes32[] memory next) {
        bool odd = layer.length % 2 == 1;
        next = new bytes32[]((layer.length + 1) / 2);
        for (uint256 i = 0; i < layer.length; i += 2) {
            bytes32 r = odd && i + 1 == layer.length ? layer[i] : layer[i + 1];
            next[i / 2] = _hashPair(layer[i], r);
        }
    }

    function _hashPair(bytes32 a, bytes32 b) private pure returns (bytes32 value) {
        /// @solidity memory-safe-assembly
        assembly {
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
