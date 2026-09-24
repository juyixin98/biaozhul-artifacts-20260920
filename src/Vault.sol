// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

/// @title Vault
/// @notice Minimal event source for the rollback indexer.
/// @dev Every state transition emits an event carrying the full delta needed to
///      reconstruct balances. The contract keeps its own on-chain state; the
///      off-chain indexer derives an independent materialized view purely from
///      events, so it can be rolled forward (apply) and backward (revert).
contract Vault {
    /// @notice Emitted on a successful deposit. `amount` is the positive delta
    ///         applied to `who`'s balance and to `totalDeposited`.
    event Deposited(address indexed who, uint256 amount);

    /// @notice Emitted on a successful withdrawal. The indexer treats the
    ///         signed delta as `-amount`; the event itself stays positive.
    event Withdrawn(address indexed who, uint256 amount);

    mapping(address => uint256) public balanceOf;
    uint256 public totalDeposited;
    uint256 public totalWithdrawn;

    error InsufficientBalance(uint256 have, uint256 want);

    function deposit() external payable {
        require(msg.value > 0, "zero deposit");
        balanceOf[msg.sender] += msg.value;
        totalDeposited += msg.value;
        emit Deposited(msg.sender, msg.value);
    }

    function withdraw(uint256 amount) external {
        uint256 have = balanceOf[msg.sender];
        if (have < amount) revert InsufficientBalance(have, amount);
        unchecked {
            balanceOf[msg.sender] = have - amount;
        }
        totalWithdrawn += amount;
        (bool ok, ) = msg.sender.call{value: amount}("");
        require(ok, "transfer failed");
        emit Withdrawn(msg.sender, amount);
    }

    /// @notice Convenience view used by the canonical-chain replay tests.
    function stats() external view returns (uint256 deposited, uint256 withdrawn) {
        return (totalDeposited, totalWithdrawn);
    }
}
