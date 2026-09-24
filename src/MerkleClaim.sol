// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {MerkleProof} from "./MerkleProof.sol";
import {BitMaps} from "./BitMaps.sol";

/// @title  MerkleClaim
/// @notice 链上 Merkle 批量领取合约。
///         叶子结构（abi.encodePacked，全部定长字段，无打包歧义）：
///             leaf = keccak256(
///                 bytes32(uint256(block.chainid)),   // 绑定链 ID，跨链重放无效
///                 bytes32(uint256(uint160(this))),   // 绑定本合约地址，跨合约重放无效
///                 uint256 index,                     // 分配表中的唯一索引
///                 address account,                   // 实际收款地址
///                 uint256 amount                     // 领取数量（wei）
///             )
///         用按索引的位图防止重复领取；批量领取在一次交易内原子完成，
///         任何一项证明无效 / 已领取 / 转账失败，整笔交易回滚，不留部分已领状态。
contract MerkleClaim {
    using BitMaps for BitMaps.BitMap;

    /// @dev 构造时登记的 Merkle 根，之后不可变
    bytes32 public immutable merkleRoot;

    /// @dev 按索引记录领取状态的位图
    BitMaps.BitMap private claimedBitmap;

    error AlreadyClaimed(uint256 index);
    error InvalidProof(uint256 index, address account, uint256 amount);
    error LengthMismatch(uint256 indexes, uint256 accounts, uint256 amounts, uint256 proofs);
    error TransferFailed(address to, uint256 amount);
    error EmptyBatch();

    event Claimed(uint256 indexed index, address indexed account, uint256 amount);

    /// @param _merkleRoot 后端根据本合约地址生成的 Merkle 根
    constructor(bytes32 _merkleRoot) payable {
        merkleRoot = _merkleRoot;
    }

    /// @notice 单笔领取
    function claim(uint256 index, address account, uint256 amount, bytes32[] calldata proof) external {
        _verifyAndMark(index, account, amount, proof);
        _sendEth(account, amount);
        emit Claimed(index, account, amount);
    }

    /// @notice 批量领取。全部验证通过后才开始转账；任一步失败整笔回滚。
    /// @dev    同一批次内重复 index 也会失败：第二次 _verifyAndMark 命中位图 → revert，
    ///         整个批次回滚（含前面已标记的位）。
    function claimBatch(
        uint256[] calldata indexes,
        address[] calldata accounts,
        uint256[] calldata amounts,
        bytes32[][] calldata proofs
    ) external {
        uint256 len = indexes.length;
        if (len == 0) revert EmptyBatch();
        if (accounts.length != len || amounts.length != len || proofs.length != len) {
            revert LengthMismatch(len, accounts.length, amounts.length, proofs.length);
        }

        // 阶段 1：先验证并标记全部叶子（纯状态检查 + 位图写），任一失败整笔交易回滚。
        for (uint256 i = 0; i < len; i++) {
            _verifyAndMark(indexes[i], accounts[i], amounts[i], proofs[i]);
        }
        // 阶段 2：全部合法后再执行外部转账。
        for (uint256 i = 0; i < len; i++) {
            _sendEth(accounts[i], amounts[i]);
            emit Claimed(indexes[i], accounts[i], amounts[i]);
        }
    }

    /// @notice 该索引是否已领取
    function isClaimed(uint256 index) external view returns (bool) {
        return claimedBitmap.get(index);
    }

    /// @dev 构造叶子 → 校验证明 → 检查并置位。chainid 和合约地址取自链上上下文，
    ///      无法由交易提交者伪造，因此从根本上阻止跨链 / 跨合约重放。
    function _verifyAndMark(
        uint256 index,
        address account,
        uint256 amount,
        bytes32[] calldata proof
    ) internal {
        if (claimedBitmap.get(index)) revert AlreadyClaimed(index);

        bytes32 leaf = keccak256(
            abi.encodePacked(
                bytes32(block.chainid),
                bytes32(uint256(uint160(address(this)))),
                index,
                account,
                amount
            )
        );
        if (!MerkleProof.verify(proof, merkleRoot, leaf)) {
            revert InvalidProof(index, account, amount);
        }

        claimedBitmap.set(index);
    }

    /// @dev 低级 call 转账：转发可用 gas 给接收方的 receive/fallback；
    ///      接收方回滚（如拒收款合约）则返回 false，整笔交易回滚。
    function _sendEth(address to, uint256 amount) private {
        (bool ok, ) = payable(to).call{value: amount}("");
        if (!ok) revert TransferFailed(to, amount);
    }

    receive() external payable {}
}
