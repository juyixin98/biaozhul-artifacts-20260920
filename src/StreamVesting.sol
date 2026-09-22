// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {IERC20} from "./interfaces/IERC20.sol";

/// @title StreamVesting
/// @notice 流式归属合约：资金按 [start, end] 线性归属，cliff 之前不可领取。
///         整数除法余量（dust）在 end 时刻一次性结清（vested(end) == amount）。
///         撤销（cancel）仅把未归属部分退回发送者，受益人保留已归属未领取额。
///         不变量：合约代币余额 == Σ(每条流的 amount - withdrawn)。
contract StreamVesting {
    // ------------------------------------------------------------------
    // 错误
    // ------------------------------------------------------------------
    error ZeroAddress();
    error ZeroAmount();
    error ZeroDuration(); // end == start
    error InvertedTimes(); // end < start
    error CliffBeforeStart(); // cliff < start
    error CliffAfterEnd(); // cliff > end
    error EndInPast(); // end <= block.timestamp
    error StreamNotFound();
    error NotSender();
    error NotBeneficiary();
    error NothingToWithdraw();
    error StreamAlreadyCancelled();
    error StreamAlreadyEnded();
    error Reentrant();
    error TransferFailed();

    // ------------------------------------------------------------------
    // 事件
    // ------------------------------------------------------------------
    event StreamCreated(
        uint256 indexed id,
        address indexed sender,
        address indexed beneficiary,
        uint128 amount,
        uint64 start,
        uint64 cliff,
        uint64 end
    );
    event Withdrawn(uint256 indexed id, address indexed beneficiary, uint128 amount);
    event StreamCancelled(uint256 indexed id, uint128 refundedToSender, uint128 vestedKept);
    event StreamToppedUp(uint256 indexed id, uint128 extra, uint128 newAmount);

    // ------------------------------------------------------------------
    // 状态
    // ------------------------------------------------------------------
    struct Stream {
        address sender;
        address beneficiary;
        uint128 amount; // 当前总额（topUp 增加；cancel 时收缩为已归属额）
        uint128 withdrawn; // 受益人已累计领取
        uint64 start;
        uint64 cliff;
        uint64 end;
        bool cancelled;
    }

    IERC20 public immutable token;
    uint256 public streamCount;
    mapping(uint256 => Stream) public streams;

    uint256 private _locked = 1;

    modifier nonReentrant() {
        if (_locked == 2) revert Reentrant();
        _locked = 2;
        _;
        _locked = 1;
    }

    constructor(IERC20 _token) {
        if (address(_token) == address(0)) revert ZeroAddress();
        token = _token;
    }

    // ------------------------------------------------------------------
    // 视图
    // ------------------------------------------------------------------

    /// @notice t 时刻的累计已归属额。cliff 前为 0；[cliff, end) 线性；end 及之后为全额（余量结清）。
    ///         已撤销的流：归属冻结在撤销时刻（amount 已收缩为 vestedAtCancel）。
    function vestedOf(uint256 id, uint64 t) public view returns (uint128) {
        Stream storage s = _get(id);
        if (s.cancelled) return s.amount;
        if (t < s.cliff) return 0;
        if (t >= s.end) return s.amount;
        unchecked {
            // amount * elapsed / duration，中间值用 uint256 防溢出
            return uint128(uint256(s.amount) * (t - s.start) / (s.end - s.start));
        }
    }

    /// @notice 当前可领取额 = 已归属 - 已领取。
    function withdrawable(uint256 id) public view returns (uint128) {
        Stream storage s = _get(id);
        return vestedOf(id, uint64(block.timestamp)) - s.withdrawn;
    }

    // ------------------------------------------------------------------
    // 状态机操作
    // ------------------------------------------------------------------

    /// @notice 创建一条归属流并全额注资。msg.sender 成为该流的 sender。
    function createStream(address beneficiary, uint128 amount, uint64 start, uint64 cliff, uint64 end)
        external
        nonReentrant
        returns (uint256 id)
    {
        if (beneficiary == address(0)) revert ZeroAddress();
        if (amount == 0) revert ZeroAmount();
        if (end < start) revert InvertedTimes();
        if (end == start) revert ZeroDuration();
        if (cliff < start) revert CliffBeforeStart();
        if (cliff > end) revert CliffAfterEnd();
        if (end <= block.timestamp) revert EndInPast();

        id = streamCount++;
        streams[id] = Stream({
            sender: msg.sender,
            beneficiary: beneficiary,
            amount: amount,
            withdrawn: 0,
            start: start,
            cliff: cliff,
            end: end,
            cancelled: false
        });

        emit StreamCreated(id, msg.sender, beneficiary, amount, start, cliff, end);
        _safeTransferFrom(token, msg.sender, address(this), amount);
    }

    /// @notice 受益人领取当前可领取额。仅受益人可调用。
    function withdraw(uint256 id) external nonReentrant {
        Stream storage s = _get(id);
        if (msg.sender != s.beneficiary) revert NotBeneficiary();
        uint128 w = vestedOf(id, uint64(block.timestamp)) - s.withdrawn;
        if (w == 0) revert NothingToWithdraw();

        s.withdrawn += w; // checks-effects-interactions：先记账再转账
        emit Withdrawn(id, msg.sender, w);
        _safeTransfer(token, msg.sender, w);
    }

    /// @notice 发送者撤销流：未归属部分退回发送者，受益人保留已归属未领取额。仅发送者可调用。
    function cancel(uint256 id) external nonReentrant {
        Stream storage s = _get(id);
        if (msg.sender != s.sender) revert NotSender();
        if (s.cancelled) revert StreamAlreadyCancelled();

        uint128 vested = vestedOf(id, uint64(block.timestamp));
        uint128 refund = s.amount - vested;

        s.amount = vested; // 归属冻结：合约内仅保留受益人的已归属权益
        s.cancelled = true;

        emit StreamCancelled(id, refund, vested);
        if (refund > 0) _safeTransfer(token, s.sender, refund);
    }

    /// @notice 发送者向未结束、未撤销的流补足资金（提高总额，归属时间表不变）。仅发送者可调用。
    function topUp(uint256 id, uint128 extra) external nonReentrant {
        Stream storage s = _get(id);
        if (msg.sender != s.sender) revert NotSender();
        if (s.cancelled) revert StreamAlreadyCancelled();
        if (block.timestamp >= s.end) revert StreamAlreadyEnded();
        if (extra == 0) revert ZeroAmount();

        s.amount += extra;
        emit StreamToppedUp(id, extra, s.amount);
        _safeTransferFrom(token, msg.sender, address(this), extra);
    }

    // ------------------------------------------------------------------
    // 内部
    // ------------------------------------------------------------------

    function _get(uint256 id) internal view returns (Stream storage s) {
        if (id >= streamCount) revert StreamNotFound();
        s = streams[id];
    }

    /// @dev 转账失败时冒泡原始 revert 数据（便于定位重入等攻击路径）。
    function _safeTransfer(IERC20 t, address to, uint256 amount) internal {
        (bool ok, bytes memory data) = address(t).call(abi.encodeCall(IERC20.transfer, (to, amount)));
        if (!ok) _bubble(data);
        if (data.length != 0 && !abi.decode(data, (bool))) revert TransferFailed();
    }

    function _safeTransferFrom(IERC20 t, address from, address to, uint256 amount) internal {
        (bool ok, bytes memory data) = address(t).call(abi.encodeCall(IERC20.transferFrom, (from, to, amount)));
        if (!ok) _bubble(data);
        if (data.length != 0 && !abi.decode(data, (bool))) revert TransferFailed();
    }

    function _bubble(bytes memory data) internal pure {
        assembly {
            revert(add(data, 32), mload(data))
        }
    }
}
