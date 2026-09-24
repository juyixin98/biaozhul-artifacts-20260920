// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

/// @title Ledger
/// @notice A minimal ledger whose three state-changing operations each emit an event.
///         An indexer can keep its materialized view in sync by applying the event
///         effects in canonical-chain order and inverting them on a rollback:
///
///             Deposited  : +amount        (inverse: -amount)
///             Withdrawn  : -amount        (inverse: +amount)
///             Transferred: from -amount, to +amount (inverse swaps sign)
///
///         Every event carries a client-chosen `tag` so tests can correlate an
///         emitted event with the chain branch it was produced on.
contract Ledger {
    mapping(address => uint256) public balances;

    event Deposited(address indexed account, uint256 amount, uint256 tag);
    event Withdrawn(address indexed account, uint256 amount, uint256 tag);
    event Transferred(address indexed from, address indexed to, uint256 amount, uint256 tag);

    error InsufficientBalance(address account, uint256 have, uint256 want);

    function deposit(uint256 amount, uint256 tag) external {
        balances[msg.sender] += amount;
        emit Deposited(msg.sender, amount, tag);
    }

    function withdraw(uint256 amount, uint256 tag) external {
        uint256 have = balances[msg.sender];
        if (have < amount) revert InsufficientBalance(msg.sender, have, amount);
        unchecked {
            balances[msg.sender] = have - amount;
        }
        emit Withdrawn(msg.sender, amount, tag);
    }

    function transfer(address to, uint256 amount, uint256 tag) external {
        uint256 fromBalance = balances[msg.sender];
        if (fromBalance < amount) revert InsufficientBalance(msg.sender, fromBalance, amount);
        unchecked {
            balances[msg.sender] = fromBalance - amount;
        }
        balances[to] += amount;
        emit Transferred(msg.sender, to, amount, tag);
    }
}
