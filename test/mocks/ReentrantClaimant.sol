// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

interface IClaim {
    function claim(uint256 index, address account, uint256 amount, bytes32[] calldata proof) external;
    function claimBatch(
        uint256[] calldata indices,
        address[] calldata accounts,
        uint256[] calldata amounts,
        bytes32[][] calldata proofs
    ) external;
}

/// @notice 重入攻击者：收到原生币时，尝试重入领取（同索引 / 不同索引两种模式）
contract ReentrantClaimant {
    IClaim public target;
    uint256 public mode; // 0=不攻击; 1=同索引重入; 2=改领另一个未领索引

    // 攻击参数
    uint256 public otherIndex;
    uint256 public otherAmount;
    bytes32[] public otherProof;
    bytes32[] public sameProof;
    uint256 public sameIndex;
    uint256 public sameAmount;
    bool public attacked; // 只在第一次收款时发动重入

    constructor(address _target) {
        target = IClaim(_target);
    }

    function configureSame(uint256 idx, uint256 amt, bytes32[] calldata p) external {
        mode = 1;
        sameIndex = idx;
        sameAmount = amt;
        sameProof = p;
    }

    function configureOther(uint256 idx, uint256 amt, bytes32[] calldata p) external {
        mode = 2;
        otherIndex = idx;
        otherAmount = amt;
        otherProof = p;
    }

    function doClaim(uint256 idx, uint256 amt, bytes32[] calldata p) external {
        target.claim(idx, address(this), amt, p);
    }

    receive() external payable {
        if (attacked) return;
        attacked = true;
        if (mode == 1) {
            // 同索引重入：位图已先于付款标记，必须失败
            target.claim(sameIndex, address(this), sameAmount, sameProof);
        } else if (mode == 2) {
            // 抢占批次中稍后才处理的索引 2；
            // 外层批次继续到索引 2 时发现已领 → AlreadyClaimed(2)，整笔回滚
            target.claim(otherIndex, address(this), otherAmount, otherProof);
        }
    }
}
