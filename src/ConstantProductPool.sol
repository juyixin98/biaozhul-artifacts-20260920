// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {IERC20} from "./interfaces/IERC20.sol";
import {CPMMMath} from "./lib/CPMMMath.sol";
import {BalanceProbe} from "./BalanceProbe.sol";

/// @title ConstantProductPool
/// @notice Two-token x*y=k AMM, Uniswap-V2-style accounting, written from
///         scratch for audit. 0.30% input fee, MINIMUM_LIQUIDITY locked to
///         zero address, floor-integer rounding that always favours the pool,
///         deadline + min-out slippage protection, strict transfer-delta
///         accounting (fee-on-transfer tokens are rejected) and a
///         non-reentrant lock around every state-changing entry point.
/// @dev Invariant held after every external state-changing call:
///         token0.balanceOf(pool) == reserve0
///         token1.balanceOf(pool) == reserve1
///      The pool therefore rejects unsolicited "donation" transfers as well
///      as tokens that deduct fees on transfer: in both cases the observed
///      balance delta differs from the nominal transfer amount.
contract ConstantProductPool {
    // ------------------------------------------------------------------
    // Errors
    // ------------------------------------------------------------------
    error SameToken();
    error ZeroAddress();
    error TokenPreFunded();
    error Expired();
    error ZeroAmount();
    error ZeroOutput();
    error ZeroRecipient();
    error NoLiquidityMinted();
    error InsufficientShares();
    error SlippageShares(uint256 shares, uint256 minShares);
    error SlippageToken(uint256 amount, uint256 minAmount);
    error InvalidTokenIn();
    error MinOutputNotMet(uint256 amountOut, uint256 minOut);
    error AmountInExceeded(uint256 paid, uint256 maxIn);
    error TransferFailed(address token);
    error UnexpectedBalanceDelta(address token, uint256 expected, uint256 actual);
    error FeeOnTransferDetected(address token, uint256 nominal, uint256 received);
    error ReservesDiverged(address token, uint256 reserve, uint256 balance);
    error ReentrancyLocked();

    // ------------------------------------------------------------------
    // Events
    // ------------------------------------------------------------------
    event Mint(address indexed sender, address indexed to, uint256 amount0, uint256 amount1, uint256 shares);
    event Burn(address indexed sender, address indexed to, uint256 amount0, uint256 amount1, uint256 shares);
    event Swap(address indexed sender, address indexed to, address tokenIn, uint256 amountIn, uint256 amountOut);
    event Sync(uint256 reserve0, uint256 reserve1);

    // ------------------------------------------------------------------
    // Immutable state
    // ------------------------------------------------------------------
    address public immutable token0;
    address public immutable token1;

    /// @dev Pool-owned measurement contract for actively detecting tokens
    ///      that short-change the recipient (see _probeToken).
    BalanceProbe public immutable probe;

    // ------------------------------------------------------------------
    // Pool state
    // ------------------------------------------------------------------
    uint112 private reserve0;
    uint112 private reserve1;
    uint256 private totalShares;

    // ------------------------------------------------------------------
    // LP ERC-20 state (shares of the pool, transferable)
    // ------------------------------------------------------------------
    string public constant name = "CPMM LP";
    string public constant symbol = "CPLP";
    uint8 public constant decimals = 18;
    mapping(address => uint256) private shareBalance;
    mapping(address => mapping(address => uint256)) private shareAllowance;

    event Approval(address indexed owner, address indexed spender, uint256 value);
    event Transfer(address indexed from, address indexed to, uint256 value);

    // ------------------------------------------------------------------
    // Reentrancy lock: 1 = unlocked, 2 = locked (gas-cheap storage slot)
    // ------------------------------------------------------------------
    uint256 private locked = 1;

    modifier nonReentrant() {
        if (locked != 1) revert ReentrancyLocked();
        locked = 2;
        _;
        locked = 1;
    }

    modifier checkDeadline(uint256 deadline) {
        if (block.timestamp > deadline) revert Expired();
        _;
    }

    constructor(address _token0, address _token1) {
        if (_token0 == address(0) || _token1 == address(0)) revert ZeroAddress();
        if (_token0 == _token1) revert SameToken();
        // Refuse to deploy on top of pre-funded assets: the invariant
        // balance == reserve must hold from the very first deposit onward.
        if (IERC20(_token0).balanceOf(address(this)) != 0 || IERC20(_token1).balanceOf(address(this)) != 0) {
            revert TokenPreFunded();
        }
        token0 = _token0;
        token1 = _token1;
        probe = new BalanceProbe();
    }

    // ==================================================================
    // Liquidity
    // ==================================================================

    /// @notice Deposit both tokens proportionally and receive LP shares.
    /// @param amount0Desired token0 amount the caller has approved
    /// @param amount1Desired token1 amount the caller has approved
    /// @param minShares revert if fewer shares are minted (slippage bound)
    /// @param to recipient of the LP shares (burn-address allowed on removal)
    /// @param deadline tx reverts after this unix timestamp
    function addLiquidity(
        uint256 amount0Desired,
        uint256 amount1Desired,
        uint256 minShares,
        address to,
        uint256 deadline
    ) external nonReentrant checkDeadline(deadline) returns (uint256 amount0, uint256 amount1, uint256 shares) {
        if (amount0Desired == 0 || amount1Desired == 0) revert ZeroAmount();
        if (to == address(0)) revert ZeroRecipient();

        (uint256 r0, uint256 r1, uint256 ts) = _getState();

        if (ts == 0) {
            // Runtime guard (defence in depth alongside the constructor
            // check): no token may already sit in the pool before the first
            // mint, otherwise strict reserve accounting could never start.
            if (IERC20(token0).balanceOf(address(this)) != 0 || IERC20(token1).balanceOf(address(this)) != 0) {
                revert TokenPreFunded();
            }
            // First deposit fixes the ratio; both desired amounts are taken.
            amount0 = amount0Desired;
            amount1 = amount1Desired;
            _takeIn(token0, msg.sender, amount0);
            _takeIn(token1, msg.sender, amount1);

            // Active fee-on-transfer screening on BOTH sides. Incoming delta
            // checks above catch sender-side fees; this probe additionally
            // measures what a recipient actually receives, catching
            // recipient-side/rebase tokens on the outgoing direction. The
            // full taken amount is probed so any non-zero fee is visible.
            _probeToken(token0, amount0);
            _probeToken(token1, amount1);

            shares = CPMMMath.sqrt(amount0 * amount1);
            if (shares <= CPMMMath.MINIMUM_LIQUIDITY) revert NoLiquidityMinted();
            // Permanent lock of the first MINIMUM_LIQUIDITY shares.
            _mintShares(address(0), CPMMMath.MINIMUM_LIQUIDITY);
            unchecked {
                shares -= CPMMMath.MINIMUM_LIQUIDITY;
            }
            _mintShares(to, shares);
        } else {
            // Take the proportional (or exact) amounts so that the depositor
            // never pays more than desired. Each side is floored in the
            // depositor's favour for what is pulled; share math floors again.
            uint256 opt1 = (amount0Desired * r1) / r0;
            if (opt1 <= amount1Desired) {
                amount0 = amount0Desired;
                amount1 = opt1;
            } else {
                uint256 opt0 = (amount1Desired * r0) / r1;
                // opt0 <= amount0Desired (floor), safe to assert ordering.
                amount0 = opt0;
                amount1 = amount1Desired;
            }
            if (amount0 == 0 || amount1 == 0) revert ZeroAmount();
            _takeIn(token0, msg.sender, amount0);
            _takeIn(token1, msg.sender, amount1);

            uint256 s0 = (amount0 * ts) / r0;
            uint256 s1 = (amount1 * ts) / r1;
            shares = s0 < s1 ? s0 : s1;
            if (shares == 0) revert InsufficientShares();
            _mintShares(to, shares);
        }

        if (shares < minShares) revert SlippageShares(shares, minShares);

        _updateAndMatch(r0 + amount0, r1 + amount1);
        emit Mint(msg.sender, to, amount0, amount1, shares);
    }

    /// @notice Burn `shares` LP tokens held by the caller and withdraw the
    ///         proportional slice of both reserves.
    /// @param shares LP shares to burn (must be the caller's own; no approval
    ///               flow needed because burning happens from msg.sender)
    /// @param minAmount0 / minAmount1 per-asset slippage bounds
    function removeLiquidity(uint256 shares, uint256 minAmount0, uint256 minAmount1, address to, uint256 deadline)
        external
        nonReentrant
        checkDeadline(deadline)
        returns (uint256 amount0, uint256 amount1)
    {
        if (shares == 0) revert ZeroAmount();
        if (to == address(0)) revert ZeroRecipient();
        (uint256 r0, uint256 r1, uint256 ts) = _getState();

        // Shares are burned before transfers (effects before interactions),
        // and the lock blocks re-entry through the outgoing token callback.
        _burnShares(msg.sender, shares);
        (amount0, amount1) = CPMMMath.calcBurnAmounts(shares, r0, r1, ts);
        if (amount0 == 0 || amount1 == 0) revert ZeroOutput();
        if (amount0 < minAmount0) revert SlippageToken(amount0, minAmount0);
        if (amount1 < minAmount1) revert SlippageToken(amount1, minAmount1);

        // MINIMUM_LIQUIDITY shares stay locked at zero, so the burned amount
        // never drains the final dust of reserves.
        _pushOut(token0, to, amount0);
        _pushOut(token1, to, amount1);

        _updateAndMatch(r0 - amount0, r1 - amount1);
        emit Burn(msg.sender, to, amount0, amount1, shares);
    }

    // ==================================================================
    // Swap
    // ==================================================================

    /// @notice Swap an exact input for a minimum output.
    /// @param tokenIn token the caller sells; must be token0 or token1
    /// @param amountIn exact input amount (pulled from msg.sender)
    /// @param minAmountOut revert if the computed output is smaller
    /// @param to recipient of the output (must not be the pool/token contracts)
    /// @param deadline tx reverts after this unix timestamp
    function swapExactInput(address tokenIn, uint256 amountIn, uint256 minAmountOut, address to, uint256 deadline)
        external
        nonReentrant
        checkDeadline(deadline)
        returns (uint256 amountOut)
    {
        if (amountIn == 0) revert ZeroAmount();
        if (to == address(0)) revert ZeroRecipient();
        (uint256 r0, uint256 r1,) = _getState();

        bool isZero;
        uint256 rIn;
        uint256 rOut;
        address tokenOut;
        if (tokenIn == token0) {
            isZero = true;
            rIn = r0;
            rOut = r1;
            tokenOut = token1;
        } else if (tokenIn == token1) {
            isZero = false;
            rIn = r1;
            rOut = r0;
            tokenOut = token0;
        } else {
            revert InvalidTokenIn();
        }
        if (to == address(this) || to == tokenIn || to == tokenOut) {
            revert ZeroRecipient();
        }

        amountOut = CPMMMath.calcAmountOut(amountIn, rIn, rOut);
        // Dust inputs (post-fee amount rounds to 0, or output rounds to 0)
        // revert: there is no fee-free/donation-style swap path.
        if (amountOut == 0) revert ZeroOutput();
        if (amountOut < minAmountOut) revert MinOutputNotMet(amountOut, minAmountOut);

        // Pull input first, then pay output; both transfers are delta checked.
        _takeIn(tokenIn, msg.sender, amountIn);
        _pushOut(tokenOut, to, amountOut);

        if (isZero) {
            _updateAndMatch(r0 + amountIn, r1 - amountOut);
        } else {
            _updateAndMatch(r0 - amountOut, r1 + amountIn);
        }
        emit Swap(msg.sender, to, tokenIn, amountIn, amountOut);
    }

    // ==================================================================
    // Sync / donation handling
    // ==================================================================

    /// @notice Reconcile reserves to the real token balances. Mints NO
    ///         shares: any tokens that arrived outside an operation
    ///         ("donations") accrue proportionally to all existing LPs.
    /// @dev Before the first mint the pool must stay empty (the constructor
    ///      already rejects pre-funding), so sync cannot bootstrap
    ///      liquidity and steal first-depositor value.
    function sync() external nonReentrant {
        if (totalShares == 0) {
            if (IERC20(token0).balanceOf(address(this)) != 0 || IERC20(token1).balanceOf(address(this)) != 0) {
                revert TokenPreFunded();
            }
        }
        _updateAndMatch(IERC20(token0).balanceOf(address(this)), IERC20(token1).balanceOf(address(this)));
    }

    // ==================================================================
    // Views
    // ==================================================================
    function getReserves() external view returns (uint112, uint112, uint256) {
        return (reserve0, reserve1, totalShares);
    }

    function totalSupply() external view returns (uint256) {
        return totalShares;
    }

    function balanceOf(address owner) external view returns (uint256) {
        return shareBalance[owner];
    }

    /// @notice Quote an output without executing (identical floor math).
    function quoteAmountOut(address tokenIn, uint256 amountIn) external view returns (uint256) {
        if (tokenIn == token0) {
            return CPMMMath.calcAmountOut(amountIn, reserve0, reserve1);
        } else if (tokenIn == token1) {
            return CPMMMath.calcAmountOut(amountIn, reserve1, reserve0);
        }
        revert InvalidTokenIn();
    }

    /// @notice Quote shares for a prospective deposit.
    function previewMintShares(uint256 amount0, uint256 amount1)
        external
        view
        returns (uint256 shares, bool firstDeposit)
    {
        (uint256 r0, uint256 r1, uint256 ts) = _getState();
        return CPMMMath.calcMintShares(amount0, amount1, r0, r1, ts);
    }

    // ==================================================================
    // LP ERC-20
    // ==================================================================
    function approve(address spender, uint256 value) external returns (bool) {
        shareAllowance[msg.sender][spender] = value;
        emit Approval(msg.sender, spender, value);
        return true;
    }

    function allowance(address owner, address spender) external view returns (uint256) {
        return shareAllowance[owner][spender];
    }

    function transfer(address to, uint256 value) external returns (bool) {
        _transferShares(msg.sender, to, value);
        return true;
    }

    function transferFrom(address from, address to, uint256 value) external returns (bool) {
        uint256 allowed = shareAllowance[from][msg.sender];
        if (allowed != type(uint256).max) {
            if (allowed < value) revert InsufficientShares();
            unchecked {
                shareAllowance[from][msg.sender] = allowed - value;
            }
        }
        _transferShares(from, to, value);
        return true;
    }

    function _transferShares(address from, address to, uint256 value) private {
        if (to == address(0)) revert ZeroAddress();
        uint256 bal = shareBalance[from];
        if (bal < value) revert InsufficientShares();
        unchecked {
            shareBalance[from] = bal - value;
            shareBalance[to] += value;
        }
        emit Transfer(from, to, value);
    }

    function _mintShares(address to, uint256 value) private {
        totalShares += value;
        unchecked {
            shareBalance[to] += value;
        }
        emit Transfer(address(0), to, value);
    }

    function _burnShares(address from, uint256 value) private {
        uint256 bal = shareBalance[from];
        if (bal < value) revert InsufficientShares();
        unchecked {
            shareBalance[from] = bal - value;
            totalShares -= value;
        }
        emit Transfer(from, address(0), value);
    }

    // ==================================================================
    // Internal: strict token transfers (reject fee-on-transfer)
    // ==================================================================

    /// @dev Pull exactly `amount` from `from`; revert if the pool's balance
    ///      does not rise by exactly `amount`. This rejects tokens that burn
    ///      a fee on transfer and any short-payment/return-false token.
    function _takeIn(address token, address from, uint256 amount) private {
        uint256 before = IERC20(token).balanceOf(address(this));
        _callToken(token, abi.encodeCall(IERC20.transferFrom, (from, address(this), amount)));
        uint256 afterBal = IERC20(token).balanceOf(address(this));
        if (afterBal != before + amount) {
            revert UnexpectedBalanceDelta(token, amount, afterBal - before);
        }
    }

    /// @dev Pay exactly `amount` to `to` and verify it on BOTH ledgers:
    ///        - the pool's balance drops by exactly `amount`
    ///        - `to`'s token balance rises by exactly `amount`
    ///      The second check rejects recipient-side fee-on-transfer tokens
    ///      (whose sender is debited in full but the recipient is credited
    ///      less) on the live swap/withdrawal path, not just at first
    ///      deposit. balanceOf is read from the token itself, so a
    ///      misbehaving recipient contract cannot fake the measurement.
    function _pushOut(address token, address to, uint256 amount) private {
        uint256 beforeMine = IERC20(token).balanceOf(address(this));
        uint256 beforeTheirs = IERC20(token).balanceOf(to);
        _callToken(token, abi.encodeCall(IERC20.transfer, (to, amount)));
        uint256 afterMine = IERC20(token).balanceOf(address(this));
        if (afterMine != beforeMine - amount) {
            revert UnexpectedBalanceDelta(token, amount, beforeMine - afterMine);
        }
        uint256 afterTheirs = IERC20(token).balanceOf(to);
        if (afterTheirs != beforeTheirs + amount) {
            revert FeeOnTransferDetected(token, amount, afterTheirs - beforeTheirs);
        }
    }

    /// @dev Low-level call accepting the ERC-20 ambiguity: non-returning
    ///      (USDT-style) or returning exactly true. Any other data reverts.
    function _callToken(address token, bytes memory data) private {
        (bool ok, bytes memory ret) = token.call(data);
        if (!ok || (ret.length != 0 && !_isTrue(ret))) {
            revert TransferFailed(token);
        }
    }

    function _isTrue(bytes memory ret) private pure returns (bool) {
        if (ret.length < 32) return false;
        bytes32 word;
        assembly {
            word := mload(add(ret, 32))
        }
        return word == bytes32(uint256(1));
    }

    /// @dev Send `amount` to the measurement probe, verify the probe
    ///      received the full nominal amount, then sweep everything back.
    ///      Rejects recipient-side fee-on-transfer / rebase tokens at first
    ///      deposit. On honest tokens the sweep returns exactly `amount`, so
    ///      the pool balance is restored with no residue in the probe.
    function _probeToken(address token, uint256 amount) private {
        // send
        _callToken(token, abi.encodeCall(IERC20.transfer, (address(probe), amount)));
        // measure recipient credit
        uint256 arrived = IERC20(token).balanceOf(address(probe));
        if (arrived != amount) {
            revert FeeOnTransferDetected(token, amount, arrived);
        }
        // return funds so post-probe pool balance is again exactly `amount`
        uint256 poolBefore = IERC20(token).balanceOf(address(this));
        probe.sweep(token, address(this));
        uint256 got = IERC20(token).balanceOf(address(this)) - poolBefore;
        if (got != amount) {
            // A token that charged a fee on the way BACK (or refused the
            // probe's transfer) is equally unsupported.
            revert FeeOnTransferDetected(token, amount, got);
        }
    }

    // ==================================================================
    // Internal: reserves
    // ==================================================================
    function _getState() private view returns (uint256, uint256, uint256) {
        return (uint256(reserve0), uint256(reserve1), totalShares);
    }

    /// @dev Commit new reserves and assert they equal the real token balances.
    ///      Any mismatch (fee-on-transfer, donation, bookkeeping bug) reverts
    ///      and unwinds the whole call atomically.
    function _updateAndMatch(uint256 newR0, uint256 newR1) private {
        if (newR0 > type(uint112).max || newR1 > type(uint112).max) {
            // Keep uint112 packing safe; revert rather than truncate.
            revert ReservesDiverged(token0, newR0, newR0);
        }
        reserve0 = uint112(newR0);
        reserve1 = uint112(newR1);
        emit Sync(uint256(reserve0), uint256(reserve1));

        if (IERC20(token0).balanceOf(address(this)) != uint256(reserve0)) {
            revert ReservesDiverged(token0, reserve0, IERC20(token0).balanceOf(address(this)));
        }
        if (IERC20(token1).balanceOf(address(this)) != uint256(reserve1)) {
            revert ReservesDiverged(token1, reserve1, IERC20(token1).balanceOf(address(this)));
        }
    }
}
