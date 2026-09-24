// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {U512} from "./U512.sol";

/// @title FeeSettlement
/// @notice Lossless fixed-rate fee accrual per block, with exact remainder carry.
///
/// @dev Fee model (integer-only, no floating point anywhere):
///
///      For one account, over `n` consecutive blocks at constant principal p
///      and rate r:
///
///          T     = carry + n * p * r
///          fees  += floor(T / SCALE)
///          carry  = T mod SCALE          (SCALE = 2^64)
///
///      `r / SCALE` is the fixed fee rate charged per block; r <= SCALE, i.e.
///      the per-block fee cannot exceed the principal. The 64-bit `carry` keeps
///      the fractional part of every unpaid fee: a dust amount smaller than
///      1/2^64 of a fee token is never burned by rounding, it rolls into the
///      next settlement. The exact identity
///
///          settle once over n blocks  ==  settle n times, one block each
///
///      follows by iterating the one-block recurrence
///          carry' + SCALE*fees' = carry + p*r
///      n times and telescoping the running totals.
///
///      Every mutating operation settles accrued fees first, so a rate or
///      principal change never applies retroactively. Settling twice in the
///      same block is a no-op, so fees are never counted twice.
contract FeeSettlement {
    using U512 for U512.U;

    /// @notice Denominator of every rate; rates are per-block integers over 2^64.
    uint256 public constant RATE_SCALE = 1 << 64;
    /// @notice Maximum rate: the fee for one block cannot exceed the principal.
    uint256 public constant MAX_RATE = RATE_SCALE;

    struct Account {
        address owner;
        uint256 principal; // fee-bearing amount, integer base units
        uint256 rate; // per-block rate numerator over RATE_SCALE, <= MAX_RATE
        uint64 lastSettleBlock; // fees are settled up to and including this block
        uint64 carry; // carried fractional fee, always < RATE_SCALE
        U512.U fees; // total settled fees, exact up to 512 bits
    }

    mapping(address => Account) private accounts;

    /// @param account fee account address
    /// @param fromBlock first block of the settled interval
    /// @param toBlock last block of the settled interval
    /// @param delta0 fee delta limb 0 (base 2^128, little-endian)
    /// @param delta1 fee delta limb 1
    /// @param delta2 fee delta limb 2
    /// @param delta3 fee delta limb 3
    event FeesSettled(
        address indexed account,
        uint256 indexed fromBlock,
        uint256 indexed toBlock,
        uint256 delta0,
        uint256 delta1,
        uint256 delta2,
        uint256 delta3
    );
    event AccountOpened(address indexed account, address indexed owner, uint256 principal, uint256 rate);
    event RateChanged(address indexed account, uint256 oldRate, uint256 newRate);
    event PrincipalChanged(address indexed account, uint256 oldPrincipal, uint256 newPrincipal);

    error AccountExists(address account);
    error NotAccountOwner(address account, address caller);
    error RateTooLarge(uint256 rate);

    modifier onlyAccountOwner(address account) {
        if (accounts[account].owner != msg.sender) revert NotAccountOwner(account, msg.sender);
        _;
    }

    /// @notice Register a fee account; accrual starts at the current block.
    function openAccount(address account, uint256 principal, uint256 rate) external {
        if (rate > MAX_RATE) revert RateTooLarge(rate);
        Account storage a = accounts[account];
        if (a.owner != address(0)) revert AccountExists(account);
        a.owner = msg.sender;
        a.principal = principal;
        a.rate = rate;
        a.lastSettleBlock = uint64(block.number);
        a.carry = 0;
        emit AccountOpened(account, msg.sender, principal, rate);
    }

    /// @notice Settle all fees accrued up to the current block.
    /// @return d0 fee delta limb 0 (base 2^128, little-endian); zero on a same-block repeat
    /// @return d1 fee delta limb 1
    /// @return d2 fee delta limb 2
    /// @return d3 fee delta limb 3
    function settle(address account)
        external
        onlyAccountOwner(account)
        returns (uint256 d0, uint256 d1, uint256 d2, uint256 d3)
    {
        (d0, d1, d2, d3) = _accrue(account, accounts[account]);
    }

    /// @notice Settle with the old rate, then install the new rate for future blocks.
    function setRate(address account, uint256 newRate)
        external
        onlyAccountOwner(account)
        returns (uint256 d0, uint256 d1, uint256 d2, uint256 d3)
    {
        if (newRate > MAX_RATE) revert RateTooLarge(newRate);
        Account storage a = accounts[account];
        (d0, d1, d2, d3) = _accrue(account, a);
        uint256 old = a.rate;
        a.rate = newRate;
        emit RateChanged(account, old, newRate);
    }

    /// @notice Settle with the old principal, then set the principal for future blocks.
    function setPrincipal(address account, uint256 newPrincipal)
        external
        onlyAccountOwner(account)
        returns (uint256 d0, uint256 d1, uint256 d2, uint256 d3)
    {
        Account storage a = accounts[account];
        (d0, d1, d2, d3) = _accrue(account, a);
        uint256 old = a.principal;
        a.principal = newPrincipal;
        emit PrincipalChanged(account, old, newPrincipal);
    }

    /// @notice Full account state; fees are returned as four 128-bit limbs (little-endian).
    function getAccount(address account)
        external
        view
        returns (
            address owner,
            uint256 principal,
            uint256 rate,
            uint256 lastSettleBlock,
            uint256 carry,
            uint256 f0,
            uint256 f1,
            uint256 f2,
            uint256 f3
        )
    {
        Account storage a = accounts[account];
        return (
            a.owner,
            a.principal,
            a.rate,
            uint256(a.lastSettleBlock),
            uint256(a.carry),
            a.fees.a0,
            a.fees.a1,
            a.fees.a2,
            a.fees.a3
        );
    }

    /// @dev Exact batched accrual over n = block.number - lastSettleBlock blocks:
    ///        T = carry + n * principal * rate   (U512 exact)
    ///        q = floor(T / SCALE), rem = T mod SCALE
    ///      fees += q; carry = rem.
    ///      If principal or rate is zero, no blocks contribute and the existing
    ///      carry is preserved exactly (zeroing it would itself lose dust).
    function _accrue(address account, Account storage a)
        private
        returns (uint256 d0, uint256 d1, uint256 d2, uint256 d3)
    {
        uint256 fromBlock = uint256(a.lastSettleBlock);
        uint256 toBlock = block.number;
        if (toBlock <= fromBlock) {
            return (0, 0, 0, 0); // same block: nothing new -> fees never counted twice
        }
        uint256 n = toBlock - fromBlock;

        U512.U memory delta;
        uint256 rem;
        uint256 p = a.principal;
        uint256 r = a.rate;
        if (p != 0 && r != 0) {
            U512.U memory total = U512.mul256(p, r); // exact p*r (<= 2^320 since r <= 2^64)
            total = U512.mulSmall(total, n); // exact *n (reverts past 512 bits)
            U512.addU256(total, uint256(a.carry)); // + carried fractional fee
            (delta, rem) = U512.divByScale(total);
            a.carry = uint64(rem);

            U512.U memory totalFees = a.fees;
            U512.addEq(totalFees, delta);
            a.fees = totalFees;
        }
        // p == 0 or r == 0: delta stays zero, carry preserved, last block advanced.
        a.lastSettleBlock = uint64(toBlock);

        d0 = delta.a0;
        d1 = delta.a1;
        d2 = delta.a2;
        d3 = delta.a3;
        emit FeesSettled(account, fromBlock, toBlock, d0, d1, d2, d3);
    }
}
