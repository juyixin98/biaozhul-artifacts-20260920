// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {IERC20} from "openzeppelin-contracts/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "openzeppelin-contracts/contracts/token/ERC20/utils/SafeERC20.sol";
import {ReentrancyGuard} from "openzeppelin-contracts/contracts/utils/ReentrancyGuard.sol";

/// @title VestingStream
/// @notice 流式归属合约：发送者(sender)为受益人(beneficiary)创建一条归属流，
///         资金在 [start, end] 区间按时间线性归属，cliff 之前归属为零，
///         整数除法余量在 end 时刻全部结清（vested(end) == amount）。
///
///         权限模型：
///           - sender      ：创建流、补足资金(topUp)、撤销(cancel)
///           - beneficiary ：领取(claim)已归属未领取的部分
///
///         撤销语义：cancel 仅收回「未归属」部分并退回 sender；
///         已归属但未领取的额度永久保留给 beneficiary，可随时 claim。
///
///         资产守恒：合约内代币余额 ==
///                   Σ(各流 amount) - Σ(各流 claimed) - Σ(各流 refunded)。
contract VestingStream is ReentrancyGuard {
    using SafeERC20 for IERC20;

    // ------------------------------------------------------------------
    // 数据结构
    // ------------------------------------------------------------------

    struct Stream {
        address sender;
        address beneficiary;
        uint128 amount; // 累计存入总额（topUp 会增加）
        uint128 claimed; // 受益人已累计领取
        uint128 refunded; // 撤销时退回 sender 的未归属部分
        uint64 start;
        uint64 cliff;
        uint64 end;
        uint64 canceledAt; // 0 = 未撤销
    }

    // ------------------------------------------------------------------
    // 事件
    // ------------------------------------------------------------------

    event StreamCreated(
        uint256 indexed id,
        address sender,
        address beneficiary,
        uint256 amount,
        uint64 start,
        uint64 cliff,
        uint64 end
    );
    event ToppedUp(uint256 indexed id, uint256 extra, uint256 newTotal);
    event Claimed(uint256 indexed id, uint256 amount);
    event Canceled(uint256 indexed id, uint256 refunded, uint256 vestedRemaining);

    // ------------------------------------------------------------------
    // 错误
    // ------------------------------------------------------------------

    error ZeroAddress();
    error ZeroAmount();
    error ZeroDuration(); // end == start
    error InvertedTimes(); // end < start
    error InvalidCliff(); // cliff < start 或 cliff > end
    error NotSender();
    error NotBeneficiary();
    error StreamCanceled();
    error NothingToClaim();

    // ------------------------------------------------------------------
    // 状态
    // ------------------------------------------------------------------

    IERC20 public immutable token;
    Stream[] internal _streams;

    constructor(IERC20 _token) {
        if (address(_token) == address(0)) revert ZeroAddress();
        token = _token;
    }

    // ------------------------------------------------------------------
    // 视图
    // ------------------------------------------------------------------

    function streamCount() external view returns (uint256) {
        return _streams.length;
    }

    function getStream(uint256 id) external view returns (Stream memory) {
        return _streams[id];
    }

    /// @notice 归属计算使用的有效时间：撤销后时间冻结在 canceledAt。
    function effectiveTime(Stream memory s) internal view returns (uint64) {
        if (s.canceledAt != 0) return s.canceledAt;
        uint64 t = uint64(block.timestamp);
        return t;
    }

    /// @notice t 时刻的已归属总额。线性归属，余量在 end 结清。
    function vestedAt(Stream memory s, uint64 t) public pure returns (uint256) {
        if (t < s.cliff) return 0;
        if (t >= s.end) return s.amount; // 整数余量在此结清
        return uint256(s.amount) * (t - s.start) / (s.end - s.start);
    }

    /// @notice 当前已归属总额（考虑撤销冻结）。
    function vested(uint256 id) public view returns (uint256) {
        Stream memory s = _streams[id];
        return vestedAt(s, effectiveTime(s));
    }

    /// @notice 当前可领取额度 = 已归属 - 已领取。
    function claimable(uint256 id) public view returns (uint256) {
        Stream memory s = _streams[id];
        return vestedAt(s, effectiveTime(s)) - s.claimed;
    }

    // ------------------------------------------------------------------
    // sender 操作
    // ------------------------------------------------------------------

    /// @notice 创建归属流并把 amount 代币转入合约托管。
    /// @dev 拒绝零时长(end==start)、倒置时间(end<start)、非法 cliff。
    function createStream(address beneficiary, uint128 amount, uint64 start, uint64 cliff, uint64 end)
        external
        nonReentrant
        returns (uint256 id)
    {
        if (beneficiary == address(0)) revert ZeroAddress();
        if (amount == 0) revert ZeroAmount();
        if (end == start) revert ZeroDuration();
        if (end < start) revert InvertedTimes();
        if (cliff < start || cliff > end) revert InvalidCliff();

        id = _streams.length;
        _streams.push(
            Stream({
                sender: msg.sender,
                beneficiary: beneficiary,
                amount: amount,
                claimed: 0,
                refunded: 0,
                start: start,
                cliff: cliff,
                end: end,
                canceledAt: 0
            })
        );

        token.safeTransferFrom(msg.sender, address(this), amount);
        emit StreamCreated(id, msg.sender, beneficiary, amount, start, cliff, end);
    }

    /// @notice 资金补足：sender 向流中追加代币，归属曲线按新总额重算。
    /// @dev 已撤销的流不可补足（未归属部分已退回）。
    function topUp(uint256 id, uint128 extra) external nonReentrant {
        Stream storage s = _streams[id];
        if (msg.sender != s.sender) revert NotSender();
        if (s.canceledAt != 0) revert StreamCanceled();
        if (extra == 0) revert ZeroAmount();

        s.amount += extra;
        token.safeTransferFrom(msg.sender, address(this), extra);
        emit ToppedUp(id, extra, s.amount);
    }

    /// @notice 撤销流：未归属部分退回 sender，已归属未领取部分保留给受益人。
    function cancel(uint256 id) external nonReentrant {
        Stream storage s = _streams[id];
        if (msg.sender != s.sender) revert NotSender();
        if (s.canceledAt != 0) revert StreamCanceled();

        uint256 vestedNow = vestedAt(s, uint64(block.timestamp));
        uint256 unvested = s.amount - vestedNow;

        s.canceledAt = uint64(block.timestamp);
        s.refunded = uint128(unvested);

        if (unvested > 0) {
            token.safeTransfer(s.sender, unvested);
        }
        emit Canceled(id, unvested, vestedNow - s.claimed);
    }

    // ------------------------------------------------------------------
    // beneficiary 操作
    // ------------------------------------------------------------------

    /// @notice 领取全部已归属未领取额度。先更新状态再转账（CEI + nonReentrant）。
    function claim(uint256 id) external nonReentrant {
        Stream storage s = _streams[id];
        if (msg.sender != s.beneficiary) revert NotBeneficiary();

        uint256 amount = vestedAt(s, effectiveTime(s)) - s.claimed;
        if (amount == 0) revert NothingToClaim();

        s.claimed += uint128(amount);
        token.safeTransfer(s.beneficiary, amount);
        emit Claimed(id, amount);
    }
}
