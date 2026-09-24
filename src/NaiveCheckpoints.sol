// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title NaiveCheckpoints
/// @notice 线性扫描参考实现：语义与 Checkpoints 完全一致
///         （同区块合并、不晚于目标块的最后值、拒绝未来块），
///         但 valueAt 使用从尾部开始的线性扫描。
///         仅用于测试中作为「参考实现」与二分实现交叉验证，
///         以及对比查询 gas 随历史长度的增长。
contract NaiveCheckpoints {
    struct Checkpoint {
        uint32 fromBlock;
        uint224 value;
    }

    Checkpoint[] private _checkpoints;

    event CheckpointUpdated(uint256 indexed blockNumber, uint256 value);

    error ValueTooLarge(uint256 value);
    error FutureBlock(uint256 requested, uint256 current);

    function setValue(uint256 value) external {
        if (value > type(uint224).max) revert ValueTooLarge(value);
        uint256 len = _checkpoints.length;
        uint32 currentBlock = uint32(block.number);
        if (len > 0 && _checkpoints[len - 1].fromBlock == currentBlock) {
            _checkpoints[len - 1].value = uint224(value);
        } else {
            _checkpoints.push(Checkpoint(currentBlock, uint224(value)));
        }
        emit CheckpointUpdated(block.number, value);
    }

    function valueAt(uint256 targetBlock) external view returns (uint256) {
        if (targetBlock > block.number) revert FutureBlock(targetBlock, block.number);
        uint256 len = _checkpoints.length;
        // 从最新检查点向后线性扫描，找到第一个 <= targetBlock 的。
        for (uint256 i = len; i > 0; --i) {
            if (_checkpoints[i - 1].fromBlock <= targetBlock) {
                return _checkpoints[i - 1].value;
            }
        }
        return 0;
    }

    function latest() external view returns (uint256 blockNumber, uint256 value) {
        uint256 len = _checkpoints.length;
        if (len == 0) return (0, 0);
        Checkpoint storage cp = _checkpoints[len - 1];
        return (cp.fromBlock, cp.value);
    }

    function length() external view returns (uint256) {
        return _checkpoints.length;
    }

    function checkpointAt(uint256 index) external view returns (uint256 blockNumber, uint256 value) {
        Checkpoint storage cp = _checkpoints[index];
        return (cp.fromBlock, cp.value);
    }
}
