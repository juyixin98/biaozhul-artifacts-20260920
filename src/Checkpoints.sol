// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

/// @title Checkpoints
/// @notice Stores a value history keyed by block number.
///
/// - Updates in the same block are merged into one checkpoint (last write wins).
/// - Historical lookups return the last checkpoint whose block is not later
///   than the requested block (upper-bound binary search, O(log n)).
/// - Queries for a block in the future (target > block.number) revert.
contract Checkpoints {
    struct Checkpoint {
        uint64 blockNumber;
        uint256 value;
    }

    /// @dev Checkpoints are stored in strictly increasing block-number order.
    Checkpoint[] private _checkpoints;

    /// @notice Emitted on every update. `merged` is true when the checkpoint
    ///         for the current block was overwritten instead of appended.
    event ValueSet(uint64 indexed blockNumber, uint256 value, bool merged);

    error FutureBlock(uint256 requestedBlock, uint256 currentBlock);

    /// @notice Records `value` at the current block. Multiple calls in the same
    ///         block overwrite the existing checkpoint rather than appending.
    function setValue(uint256 value) external {
        uint256 current = block.number;
        uint256 len = _checkpoints.length;
        bool merged;
        if (len > 0 && _checkpoints[len - 1].blockNumber == current) {
            _checkpoints[len - 1].value = value;
            merged = true;
        } else {
            _checkpoints.push(Checkpoint(uint64(current), value));
        }
        emit ValueSet(uint64(current), value, merged);
    }

    /// @notice Number of stored checkpoints (one per touched block).
    function length() external view returns (uint256) {
        return _checkpoints.length;
    }

    /// @notice Raw checkpoint by index; useful for inspection / oracles.
    function checkpointAt(uint256 index)
        external
        view
        returns (uint64 blockNumber, uint256 value)
    {
        Checkpoint storage cp = _checkpoints[index];
        return (cp.blockNumber, cp.value);
    }

    /// @notice Most recent checkpoint, if any.
    function latest()
        external
        view
        returns (bool exists, uint256 blockNumber, uint256 value)
    {
        uint256 len = _checkpoints.length;
        if (len == 0) {
            return (false, 0, 0);
        }
        Checkpoint storage cp = _checkpoints[len - 1];
        return (true, uint256(cp.blockNumber), cp.value);
    }

    /// @notice Returns the last checkpoint at or before `targetBlock`.
    /// @dev Upper-bound binary search: largest index i with
    ///      _checkpoints[i].blockNumber <= targetBlock, answer is i - 1.
    ///      Reverts if targetBlock is greater than the current block.
    function getAtBlock(uint256 targetBlock)
        external
        view
        returns (bool exists, uint256 blockNumber, uint256 value)
    {
        if (targetBlock > block.number) {
            revert FutureBlock(targetBlock, block.number);
        }

        uint256 lo = 0;
        uint256 hi = _checkpoints.length;
        while (lo < hi) {
            uint256 mid = (lo + hi) >> 1;
            if (_checkpoints[mid].blockNumber <= targetBlock) {
                lo = mid + 1;
            } else {
                hi = mid;
            }
        }

        if (lo == 0) {
            // No checkpoint at or before the target (empty history or target
            // earlier than the first checkpoint).
            return (false, 0, 0);
        }

        Checkpoint storage cp = _checkpoints[lo - 1];
        return (true, uint256(cp.blockNumber), cp.value);
    }
}
