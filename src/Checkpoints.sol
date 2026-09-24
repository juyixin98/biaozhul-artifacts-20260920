// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title Checkpoints
/// @notice 按区块保存数值检查点。同一区块内的多次更新会合并（后写覆盖），
///         每个区块最多保留一个检查点。历史不可篡改：已结束区块的检查点
///         不会被修改。查询返回「不晚于目标区块的最后一个值」，
///         并拒绝查询未来区块。
contract Checkpoints {
    /// @dev 区块号用 uint32 打包，数值用 uint224 打包（单个存储槽）。
    struct Checkpoint {
        uint32 fromBlock;
        uint224 value;
    }

    /// @dev 单调递增的检查点数组，fromBlock 严格递增。
    Checkpoint[] private _checkpoints;

    /// @dev 仅部署者可以写入；测试时若需任意账户写入可改为 permissionless。
    address public immutable owner;

    event CheckpointUpdated(uint256 indexed blockNumber, uint256 value);

    error NotOwner();
    error ValueTooLarge(uint256 value);
    error FutureBlock(uint256 requested, uint256 current);

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    constructor() {
        owner = msg.sender;
    }

    // ---------------------------------------------------------------------
    // 写入
    // ---------------------------------------------------------------------

    /// @notice 在当前区块记录数值检查点；同区块重复调用以最后一次为准。
    function setValue(uint256 value) external onlyOwner {
        if (value > type(uint224).max) revert ValueTooLarge(value);
        uint256 len = _checkpoints.length;
        uint32 currentBlock = uint32(block.number);
        if (len > 0 && _checkpoints[len - 1].fromBlock == currentBlock) {
            // 同区块更新合并：覆盖最后一个检查点，不新增。
            _checkpoints[len - 1].value = uint224(value);
        } else {
            _checkpoints.push(Checkpoint(currentBlock, uint224(value)));
        }
        emit CheckpointUpdated(block.number, value);
    }

    // ---------------------------------------------------------------------
    // 读取
    // ---------------------------------------------------------------------

    /// @notice 返回不晚于 targetBlock 的最后一个检查点值。
    ///         不存在（目标区块早于第一个检查点，或没有任何检查点）时返回 0。
    /// @dev 对内部严格递增的 fromBlock 数组做二分查找（上界）。
    function valueAt(uint256 targetBlock) external view returns (uint256) {
        if (targetBlock > block.number) {
            revert FutureBlock(targetBlock, block.number);
        }
        uint256 len = _checkpoints.length;
        if (len == 0 || _checkpoints[0].fromBlock > targetBlock) {
            return 0;
        }

        // 不变量：
        //   lo 始终指向 fromBlock <= targetBlock 的位置（初始 0 已满足）
        //   hi 始终指向 fromBlock >  targetBlock 的位置（哨兵 len 满足）
        uint256 lo = 0;
        uint256 hi = len;
        while (lo + 1 < hi) {
            uint256 mid = (lo + hi) / 2;
            if (_checkpoints[mid].fromBlock <= targetBlock) {
                lo = mid;
            } else {
                hi = mid;
            }
        }
        return _checkpoints[lo].value;
    }

    /// @notice 最新一个检查点；没有任何检查点时返回 (0, 0)。
    function latest() external view returns (uint256 blockNumber, uint256 value) {
        uint256 len = _checkpoints.length;
        if (len == 0) return (0, 0);
        Checkpoint storage cp = _checkpoints[len - 1];
        return (cp.fromBlock, cp.value);
    }

    /// @notice 已保存的检查点数量（同区块合并后）。
    function length() external view returns (uint256) {
        return _checkpoints.length;
    }

    /// @notice 按索引读取检查点，便于链下核对。
    function checkpointAt(uint256 index) external view returns (uint256 blockNumber, uint256 value) {
        Checkpoint storage cp = _checkpoints[index];
        return (cp.fromBlock, cp.value);
    }
}
