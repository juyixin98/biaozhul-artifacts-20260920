// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Math} from "../lib/openzeppelin/contracts/utils/math/Math.sol";

/// @title LosslessFeeSettlement
/// @notice Per-account fixed-rate fee accrual that never permanently loses dust:
///         the truncated integer remainder of every settlement is carried into
///         the next one.
/// @dev All amounts are integer token base units (no decimals() reliance).
///
///      A per-block fee for principal P and rate R (mantissa, 1e18 == 100%/block)
///      across n blocks is
///
///          numerator = P * R * n + carry
///          fee       = numerator / 1e18        (credited, floor)
///          carry'    = numerator mod 1e18      (remembered)
///
///      P*R can exceed 2^256, so the 512-bit Math.mulDiv / mulmod are used.
///      Batched settlement over n blocks is provably equal to n per-block
///      settlements with carry: the integer identity
///          (a+b+c) div/rem d == chained div/rem with carried remainder
///      makes the "settle once over 100 blocks" and "settle every block"
///      results identical (fee total AND remainder).
contract LosslessFeeSettlement {
    using Math for uint256;

    uint256 public constant RATE_SCALE = 1e18;

    struct Account {
        uint256 principal;       // fee-bearing base amount
        uint256 ratePerBlock;    // mantissa over RATE_SCALE
        uint256 lastBlock;       // last block included in a settlement
        uint256 accruedFees;     // total fees settled (claimed or pending)
        uint256 remainder;       // dust carried to the next settlement
    }

    mapping(address => Account) private accounts;

    event Opened(address indexed account, uint256 principal, uint256 ratePerBlock, uint256 startBlock);
    event Settled(address indexed account, uint256 blocks, uint256 feeAdded, uint256 newRemainder, uint256 totalAccrued);
    event RateChanged(address indexed account, uint256 oldRate, uint256 newRate);
    event PrincipalChanged(address indexed account, uint256 oldPrincipal, uint256 newPrincipal);
    event Claimed(address indexed account, uint256 amount);
    event Closed(address indexed account, uint256 finalFee);

    error NotOpen();
    error AlreadyOpen();
    error NothingDue();
    error RateExceedsScale(uint256 rate);

    /// @notice Open an account. Fees start accruing at the NEXT block
    ///         (block.number + 1), so no fee is ever charged for the
    ///         opening block regardless of intra-block ordering.
    function open(uint256 principal, uint256 ratePerBlock_) external {
        Account storage a = accounts[msg.sender];
        if (a.lastBlock != 0 || a.principal != 0 || a.accruedFees != 0 || a.remainder != 0) {
            revert AlreadyOpen();
        }
        if (ratePerBlock_ > RATE_SCALE) revert RateExceedsScale(ratePerBlock_);
        a.principal = principal;
        a.ratePerBlock = ratePerBlock_;
        a.lastBlock = block.number;
        emit Opened(msg.sender, principal, ratePerBlock_, block.number);
    }

    /// @notice Accrue fees through the current block (n = block.number - lastBlock)
    ///         and return the fee credited in this call.
    /// @dev n == 0 is a legitimate no-op (e.g. repeated settle in one block);
    ///      it returns 0 and leaves state untouched, which is also what makes
    ///      fees immune to double counting.
    function settle() public returns (uint256 feeAdded) {
        Account storage a = accounts[msg.sender];
        if (a.lastBlock == 0) revert NotOpen();
        uint256 n = block.number - a.lastBlock;
        if (n == 0) {
            return 0;
        }

        // Per-block product, full 512-bit precision:
        // per = principal * ratePerBlock / SCALE, rem = principal * ratePerBlock % SCALE
        uint256 per = a.principal.mulDiv(a.ratePerBlock, RATE_SCALE);
        uint256 rem = mulmod(a.principal, a.ratePerBlock, RATE_SCALE);

        // Total over n blocks plus the dust carried from history.
        // Checked on purpose: inputs so extreme that the result exceeds
        // 2^256 must revert loudly instead of silently wrapping.
        uint256 remTotal = rem * n + a.remainder;
        feeAdded = per * n + remTotal / RATE_SCALE;
        a.remainder = remTotal % RATE_SCALE;
        a.accruedFees += feeAdded;
        a.lastBlock = block.number;

        emit Settled(msg.sender, n, feeAdded, a.remainder, a.accruedFees);
    }

    /// @notice Settle before changing the rate, so fees earned under the old
    ///         rate are never re-priced.
    function setRate(uint256 newRate) external {
        if (newRate > RATE_SCALE) revert RateExceedsScale(newRate);
        Account storage a = accounts[msg.sender];
        if (a.lastBlock == 0) revert NotOpen();
        settle();
        uint256 old = a.ratePerBlock;
        a.ratePerBlock = newRate;
        emit RateChanged(msg.sender, old, newRate);
    }

    /// @notice Settle before changing principal, so the new base amount only
    ///         applies from the next block onward.
    function setPrincipal(uint256 newPrincipal) external {
        Account storage a = accounts[msg.sender];
        if (a.lastBlock == 0) revert NotOpen();
        settle();
        uint256 old = a.principal;
        a.principal = newPrincipal;
        emit PrincipalChanged(msg.sender, old, newPrincipal);
    }

    /// @notice Settle and push accrued fees to the caller.
    /// @dev In a tokenized deployment this would transfer an ERC20; here fees
    ///      are a tracked balance in the same integer units.
    function claim() external returns (uint256 amount) {
        Account storage a = accounts[msg.sender];
        if (a.lastBlock == 0) revert NotOpen();
        settle();
        amount = a.accruedFees;
        if (amount == 0) revert NothingDue();
        a.accruedFees = 0;
        emit Claimed(msg.sender, amount);
    }

    /// @notice Settle through the current block and freeze the account by
    ///         zeroing principal and rate. The account cannot be reopened
    ///         (use a fresh address); the final sub-unit remainder stays in
    ///         storage readable via getAccount().
    function close() external returns (uint256 finalFee) {
        Account storage a = accounts[msg.sender];
        if (a.lastBlock == 0) revert NotOpen();
        finalFee = settle();
        a.principal = 0;
        a.ratePerBlock = 0;
        emit Closed(msg.sender, finalFee);
    }

    function getAccount(address account)
        external
        view
        returns (
            uint256 principal,
            uint256 ratePerBlock_,
            uint256 lastBlock,
            uint256 accruedFees,
            uint256 remainder,
            uint256 pendingBlocks,
            uint256 projectedFee,
            uint256 projectedRemainder
        )
    {
        Account storage a = accounts[account];
        principal = a.principal;
        ratePerBlock_ = a.ratePerBlock;
        lastBlock = a.lastBlock;
        accruedFees = a.accruedFees;
        remainder = a.remainder;
        if (a.lastBlock != 0 && block.number >= a.lastBlock) {
            pendingBlocks = block.number - a.lastBlock;
        }
        if (pendingBlocks != 0) {
            uint256 per = principal.mulDiv(ratePerBlock_, RATE_SCALE);
            uint256 rem = mulmod(principal, ratePerBlock_, RATE_SCALE);
            uint256 remTotal = rem * pendingBlocks + remainder;
            projectedFee = accruedFees + per * pendingBlocks + remTotal / RATE_SCALE;
            projectedRemainder = remTotal % RATE_SCALE;
        } else {
            projectedFee = accruedFees;
            projectedRemainder = remainder;
        }
    }

    /// @notice Pure reference of the integer-exponent settlement step, useful
    ///         for tests and off-chain reconciliation.
    function quote(uint256 principal_, uint256 ratePerBlock_, uint256 blocks, uint256 carryIn)
        external
        pure
        returns (uint256 feeAdded, uint256 carryOut)
    {
        uint256 per = principal_.mulDiv(ratePerBlock_, RATE_SCALE);
        uint256 rem = mulmod(principal_, ratePerBlock_, RATE_SCALE);
        uint256 remTotal = rem * blocks + carryIn;
        feeAdded = per * blocks + remTotal / RATE_SCALE;
        carryOut = remTotal % RATE_SCALE;
    }
}
