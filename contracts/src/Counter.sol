// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/// @title Counter —— 多签执行器的示例/测试目标合约
/// @notice 记录自增调用次数；并支持基于时间的“先失败后成功”模式，
///         用于验证目标失败后的重试规则。
/// @dev 注意：目标调用一旦 revert，其本次状态变更会随子调用一起回滚，
///      因此不能用“目标自身计数”模拟失败次数（计数也会被回滚）。
///      这里改用 block.timestamp：succeedAfter 之前调用一律失败，
///      到点后成功，真实反映“目标条件在重试之间被修复”的场景。
contract Counter {
    uint256 public count;
    address public immutable wallet;

    /// @dev 早于该时间戳的 increment 调用都会 revert；0 表示从不失败。
    uint64 public succeedAfter;

    event Incremented(uint256 newCount, address caller);

    constructor(address _wallet, uint64 _succeedAfter) {
        wallet = _wallet;
        succeedAfter = _succeedAfter;
    }

    function setSucceedAfter(uint64 ts) external {
        succeedAfter = ts;
    }

    function increment() external returns (uint256) {
        if (succeedAfter != 0 && block.timestamp < succeedAfter) {
            revert("flaky target: failing on purpose");
        }
        count++;
        emit Incremented(count, msg.sender);
        return count;
    }

    /// @notice 纯成功回调，用于验证执行回调返回数据。
    function echo(uint256 x) external pure returns (uint256) {
        return x;
    }
}
