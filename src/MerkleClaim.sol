// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {MerkleProof} from "./MerkleProof.sol";

interface IERC20 {
    function transfer(address to, uint256 amount) external returns (bool);
    function balanceOf(address account) external view returns (uint256);
}

/// @title  MerkleClaim
/// @notice 叶子绑定 (chainId, contractAddress, index, account, amount) 的批量领取合约，
///         用按索引位图防重复，批量领取整体原子（任一单项无效则整笔回滚，不留部分已领状态）。
/// @dev    防重放设计：
///         - chainId 绑定：部署时的 chainId 写入 immutable，叶子必须匹配该 chainId，
///           在另一条链（chainId 不同）部署的同字节码合约上，原链叶子全部失效；
///         - 合约地址绑定：叶子哈希包含 address(this)，同链上部署第二个合约实例时，
///           第一个实例的证明无法在第二个实例重放；
///         - 按索引位图：claim 与 claimBatch 共用同一套标记逻辑，
///           同一笔批次内重复索引在标记第二位时直接回滚。
contract MerkleClaim {
    // ---------------------------------------------------------------------
    // 存储
    // ---------------------------------------------------------------------

    bytes32 public immutable merkleRoot;
    uint256 public immutable deployChainId;
    address public immutable token; // address(0) 表示发放原生币（ETH）
    address public immutable owner;

    /// @dev claimedBitmap[index] 的第 (index % 256) 位表示 index 是否已领。
    mapping(uint256 => uint256) private _claimedBitmap;

    // ---------------------------------------------------------------------
    // 事件
    // ---------------------------------------------------------------------

    event Claimed(uint256 indexed index, address indexed account, uint256 amount);
    event BatchClaimed(uint256 count, uint256 totalAmount);

    // ---------------------------------------------------------------------
    // 错误
    // ---------------------------------------------------------------------

    error AlreadyClaimed(uint256 index);
    error InvalidProof(uint256 index, address account, uint256 amount);
    error WrongChain(uint256 expected, uint256 actual);
    error EmptyBatch();
    error LengthMismatch(uint256 indices, uint256 accounts, uint256 amounts, uint256 proofs);
    error NativeTransferFailed(address to, uint256 amount);
    error TokenTransferFailed(address to, uint256 amount);
    error OnlyOwner();
    error NothingToWithdraw();

    // ---------------------------------------------------------------------
    // 构造
    // ---------------------------------------------------------------------

    /// @param root  叶子树 Merkle 根
    /// @param token_ ERC20 地址；address(0) 表示原生币模式
    /// @param owner_ 紧急提款接收人（默认 msg.sender）
    constructor(bytes32 root, address token_, address owner_) payable {
        merkleRoot = root;
        token = token_;
        owner = owner_ == address(0) ? msg.sender : owner_;
        deployChainId = block.chainid;
    }

    // ---------------------------------------------------------------------
    // 叶子编码
    // ---------------------------------------------------------------------

    /// @notice 叶子哈希：绑定链 ID、合约地址、索引、账户、数量。
    /// @dev    keccak256(abi.encodePacked(chainid, address(this), index, account, amount))
    ///         链下（Python）必须按完全相同的字段顺序与 packed 编码生成。
    ///         固定大小字段（uint256/address），packed 无填充，与 encode 长度相同但更省 gas。
    function leafHash(uint256 index, address account, uint256 amount) public view returns (bytes32) {
        return keccak256(abi.encodePacked(block.chainid, address(this), index, account, amount));
    }

    // ---------------------------------------------------------------------
    // 视图
    // ---------------------------------------------------------------------

    /// @notice 索引 index 是否已领取
    function isClaimed(uint256 index) public view returns (bool) {
        return (_claimedBitmap[index >> 8] & (1 << (index & 0xff))) != 0;
    }

    /// @notice 位图原语：第 wordIndex 个 256 位字
    function claimedWord(uint256 wordIndex) external view returns (uint256) {
        return _claimedBitmap[wordIndex];
    }

    // ---------------------------------------------------------------------
    // 领取
    // ---------------------------------------------------------------------

    /// @notice 单项领取
    function claim(uint256 index, address account, uint256 amount, bytes32[] calldata proof) external {
        _verifyAndMark(index, account, amount, proof);
        _payout(account, amount);
        emit Claimed(index, account, amount);
    }

    /// @notice 批量领取：全部项目先逐一校验并打位图标记，然后才逐一付款。
    /// @dev 原子性来源（EVM 事务语义 + 本函数顺序）：
    ///      1. 任一叶子证明错误 / 重复 / 跨链 → require 失败，整笔交易回滚，
    ///         已执行的位图标记、付款全部撤销；
    ///      2. 全部标记完成后才付款，付款采用 call（原生币）/ transfer（ERC20），
    ///         接收方合约的回调发生时，本批次所有索引都已标记，重入无法重复领取。
    function claimBatch(
        uint256[] calldata indices,
        address[] calldata accounts,
        uint256[] calldata amounts,
        bytes32[][] calldata proofs
    ) external {
        uint256 n = indices.length;
        if (n == 0) revert EmptyBatch();
        if (accounts.length != n || amounts.length != n || proofs.length != n) {
            revert LengthMismatch(n, accounts.length, amounts.length, proofs.length);
        }
        if (block.chainid != deployChainId) revert WrongChain(deployChainId, block.chainid);

        // 阶段 1：全部校验 + 标记（checks & effects），不做任何外部调用
        uint256 total;
        for (uint256 i = 0; i < n; i++) {
            _verifyAndMark(indices[i], accounts[i], amounts[i], proofs[i]);
            total += amounts[i];
        }

        // 阶段 2：付款（interactions）
        for (uint256 i = 0; i < n; i++) {
            _payout(accounts[i], amounts[i]);
            emit Claimed(indices[i], accounts[i], amounts[i]);
        }
        emit BatchClaimed(n, total);
    }

    // ---------------------------------------------------------------------
    // 内部
    // ---------------------------------------------------------------------

    function _verifyAndMark(
        uint256 index,
        address account,
        uint256 amount,
        bytes32[] calldata proof
    ) internal {
        if (block.chainid != deployChainId) revert WrongChain(deployChainId, block.chainid);

        // 1) 位图防重（跨交易 + 同批次重复索引都在此拦截）
        uint256 wordIndex = index >> 8;
        uint256 mask = 1 << (index & 0xff);
        if (_claimedBitmap[wordIndex] & mask != 0) revert AlreadyClaimed(index);

        // 2) 叶子绑定 (chainId, address(this), index, account, amount)
        bytes32 leaf = keccak256(abi.encodePacked(block.chainid, address(this), index, account, amount));
        if (!MerkleProof.verify(proof, merkleRoot, leaf)) {
            revert InvalidProof(index, account, amount);
        }

        // 3) 打标记（先于付款，防重入）
        _claimedBitmap[wordIndex] |= mask;
    }

    function _payout(address to, uint256 amount) internal {
        if (token == address(0)) {
            (bool ok, ) = payable(to).call{value: amount}("");
            if (!ok) revert NativeTransferFailed(to, amount);
        } else {
            try IERC20(token).transfer(to, amount) returns (bool ok) {
                if (!ok) revert TokenTransferFailed(to, amount);
            } catch {
                revert TokenTransferFailed(to, amount);
            }
        }
    }

    // ---------------------------------------------------------------------
    // 管理：紧急回收未领取资金（不影响已打标记，仅 owner 可调用）
    // ---------------------------------------------------------------------

    function withdraw(uint256 amount, bool isToken) external {
        if (msg.sender != owner) revert OnlyOwner();
        if (isToken) {
            if (IERC20(token).balanceOf(address(this)) < amount) revert NothingToWithdraw();
            if (!IERC20(token).transfer(owner, amount)) revert TokenTransferFailed(owner, amount);
        } else {
            if (address(this).balance < amount) revert NothingToWithdraw();
            (bool ok, ) = payable(owner).call{value: amount}("");
            if (!ok) revert NativeTransferFailed(owner, amount);
        }
    }

    receive() external payable {}
}
