// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {MerkleProof} from "../../src/MerkleProof.sol";

/// @notice 测试辅助：用与链下一致的规则（有序配对、奇数位复制最后节点）构造 Merkle 树并生成证明
contract TreeBuilder {
    struct Level {
        bytes32[] nodes;
    }

    bytes32[] public leaves;
    mapping(uint256 => uint256) internal _leafPos; // 业务索引 => 叶子下标
    Level[] internal _levels; // 0 = 叶子层，末层 = 根
    bool _built;

    function addLeaf(uint256 index, bytes32 leaf) external {
        require(!_built, "built");
        _leafPos[index] = leaves.length;
        leaves.push(leaf);
    }

    function root() external returns (bytes32) {
        _build();
        return _levels[_levels.length - 1].nodes[0];
    }

    /// @dev 返回业务索引对应叶子的证明（兄弟节点；配对排序由 MerkleProof 完成）
    function proofFor(uint256 index) external returns (bytes32[] memory) {
        _build();
        uint256 pos = _leafPos[index];
        bytes32[] memory tmp = new bytes32[](_levels.length);
        uint256 cnt;

        for (uint256 lv = 0; lv < _levels.length - 1; lv++) {
            uint256 size = _levels[lv].nodes.length;
            uint256 sib = pos ^ 1;
            if (sib < size) {
                tmp[cnt++] = _levels[lv].nodes[sib];
            } else {
                // 奇数位：最后一个节点被直接提升，兄弟即自身
                tmp[cnt++] = _levels[lv].nodes[pos];
            }
            pos >>= 1;
        }

        bytes32[] memory p = new bytes32[](cnt);
        for (uint256 i = 0; i < cnt; i++) p[i] = tmp[i];
        return p;
    }

    function _build() internal {
        if (_built) return;
        _built = true;
        require(leaves.length > 0, "empty tree");

        Level storage lvl0 = _levels.push();
        for (uint256 i = 0; i < leaves.length; i++) lvl0.nodes.push(leaves[i]);

        while (_levels[_levels.length - 1].nodes.length > 1) {
            bytes32[] storage cur = _levels[_levels.length - 1].nodes;
            Level storage next = _levels.push();
            uint256 nextSize = (cur.length + 1) / 2;
            for (uint256 i = 0; i < nextSize; i++) {
                bytes32 a = cur[2 * i];
                bytes32 b = (2 * i + 1 < cur.length) ? cur[2 * i + 1] : a;
                next.nodes.push(MerkleProof.hashPair(a, b));
            }
        }
    }
}
