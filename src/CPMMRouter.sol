// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {ERC20} from "./ERC20.sol";
import {CPMMFactory} from "./CPMMFactory.sol";
import {CPMMPair} from "./CPMMPair.sol";
import {CPMMMath} from "./CPMMMath.sol";

interface IERC20Meta {
    function transfer(address, uint256) external returns (bool);
    function transferFrom(address, address, uint256) external returns (bool);
}

/// @title Stateless router around CPMMPair.
/// @notice Adds deadline + slippage protection and pair lookup. Fee-on-transfer
///         tokens are rejected: the amount received by the pair must equal the
///         nominal input amount on every deposit and every swap hop.
contract CPMMRouter {
    CPMMFactory public immutable factory;

    error Expired();
    error Slippage();
    error ZeroAddress();
    error InvalidPath();
    error FeeOnTransferDetected();
    error TransferFailed();

    modifier ensure(uint256 deadline) {
        if (block.timestamp > deadline) revert Expired();
        _;
    }

    constructor(address _factory) {
        factory = CPMMFactory(_factory);
    }

    // ------------------------------------------------------------------
    // Add liquidity
    // ------------------------------------------------------------------

    /// @return amountA / amountB actual amounts consumed, liquidity shares minted.
    function addLiquidity(
        address tokenA,
        address tokenB,
        uint256 amountADesired,
        uint256 amountBDesired,
        uint256 amountAMin,
        uint256 amountBMin,
        address to,
        uint256 deadline
    ) external ensure(deadline) returns (uint256 amountA, uint256 amountB, uint256 liquidity) {
        if (to == address(0)) revert ZeroAddress();
        address pair = factory.getPair(tokenA, tokenB);
        if (pair == address(0)) {
            pair = factory.createPair(tokenA, tokenB);
        }
        address token0 = address(CPMMPair(pair).token0());

        (uint112 reserve0, uint112 reserve1,) = CPMMPair(pair).getReserves();
        // Bootstrap on ZERO reserves, not merely on pair existence: a pair can
        // already be deployed while still empty.
        if (reserve0 == 0 || reserve1 == 0) {
            (amountA, amountB) = (amountADesired, amountBDesired);
        } else {
            (uint256 reserveA, uint256 reserveB) =
                tokenA == token0 ? (uint256(reserve0), uint256(reserve1)) : (uint256(reserve1), uint256(reserve0));
            // Deposit at the current ratio; choose the bound that cannot be
            // gamed (floor on the quoted side, accept at most the desired side).
            uint256 amountBOptimal = CPMMMath.quote(amountADesired, reserveA, reserveB);
            if (amountBOptimal <= amountBDesired) {
                (amountA, amountB) = (amountADesired, amountBOptimal);
            } else {
                uint256 amountAOptimal = CPMMMath.quote(amountBDesired, reserveB, reserveA);
                assert(amountAOptimal <= amountADesired);
                (amountA, amountB) = (amountAOptimal, amountBDesired);
            }
        }

        if (amountA < amountAMin || amountB < amountBMin) revert Slippage();

        _pull(tokenA, msg.sender, pair, amountA);
        _pull(tokenB, msg.sender, pair, amountB);
        // mint expects amounts in the pair's token0/token1 order.
        (uint256 amount0, uint256 amount1) = tokenA == token0 ? (amountA, amountB) : (amountB, amountA);
        liquidity = CPMMPair(pair).mint(to, amount0, amount1);
    }

    // ------------------------------------------------------------------
    // Remove liquidity
    // ------------------------------------------------------------------

    /// @notice Burns LP shares and returns both tokens to `to`.
    function removeLiquidity(
        address tokenA,
        address tokenB,
        uint256 liquidity,
        uint256 amountAMin,
        uint256 amountBMin,
        address to,
        uint256 deadline
    ) external ensure(deadline) returns (uint256 amountA, uint256 amountB) {
        if (to == address(0)) revert ZeroAddress();
        (address token0,) = _sortTokens(tokenA, tokenB);
        address pair = factory.getPair(tokenA, tokenB);
        if (pair == address(0)) revert InvalidPath();

        // Move LP shares to the pair where burn() reads them.
        if (!IERC20Meta(pair).transferFrom(msg.sender, pair, liquidity)) revert TransferFailed();
        (uint256 amount0, uint256 amount1) = CPMMPair(pair).burn(to);
        (amountA, amountB) = tokenA == token0 ? (amount0, amount1) : (amount1, amount0);
        if (amountA < amountAMin || amountB < amountBMin) revert Slippage();
    }

    // ------------------------------------------------------------------
    // Swap
    // ------------------------------------------------------------------

    /// @notice Exact-input multi-hop swap. amountOutMin and deadline are mandatory.
    function swapExactTokensForTokens(
        uint256 amountIn,
        uint256 amountOutMin,
        address[] calldata path,
        address to,
        uint256 deadline
    ) external ensure(deadline) returns (uint256[] memory amounts) {
        if (to == address(0)) revert ZeroAddress();
        if (path.length < 2) revert InvalidPath();
        amounts = _getAmountsOut(amountIn, path);
        if (amounts[amounts.length - 1] < amountOutMin) revert Slippage();

        _pull(path[0], msg.sender, factory.getPair(path[0], path[1]), amountIn);

        // Single-hop and multi-hop share the same loop. Intermediate tokens
        // land directly in the next pair via `to = nextPair`, which rejects
        // fee-on-transfer intermediates (balance delta would be short).
        for (uint256 i; i < path.length - 1; ++i) {
            (address token0,) = _sortTokens(path[i], path[i + 1]);
            uint256 amount0Out;
            uint256 amount1Out;
            address recipient = i + 2 < path.length ? factory.getPair(path[i + 1], path[i + 2]) : to;
            (amount0Out, amount1Out) = path[i] == token0 ? (uint256(0), amounts[i + 1]) : (amounts[i + 1], uint256(0));
            address pair = factory.getPair(path[i], path[i + 1]);
            (uint256 expectedInput0, uint256 expectedInput1) =
                path[i] == token0 ? (amounts[i], uint256(0)) : (uint256(0), amounts[i]);
            CPMMPair(pair).swap(amount0Out, amount1Out, recipient, expectedInput0, expectedInput1, "");
        }
    }

    // ------------------------------------------------------------------
    // Views
    // ------------------------------------------------------------------

    function getAmountOut(uint256 amountIn, address tokenIn, address tokenOut) external view returns (uint256) {
        address pair = factory.getPair(tokenIn, tokenOut);
        (uint112 reserve0, uint112 reserve1,) = CPMMPair(pair).getReserves();
        (uint256 reserveIn, uint256 reserveOut) =
            tokenIn < tokenOut ? (uint256(reserve0), uint256(reserve1)) : (uint256(reserve1), uint256(reserve0));
        return CPMMMath.getAmountOut(amountIn, reserveIn, reserveOut);
    }

    function _getAmountsOut(uint256 amountIn, address[] calldata path) private view returns (uint256[] memory amounts) {
        amounts = new uint256[](path.length);
        amounts[0] = amountIn;
        for (uint256 i; i < path.length - 1; ++i) {
            address pair = factory.getPair(path[i], path[i + 1]);
            (uint112 reserve0, uint112 reserve1,) = CPMMPair(pair).getReserves();
            (address token0,) = _sortTokens(path[i], path[i + 1]);
            (uint256 reserveIn, uint256 reserveOut) =
                path[i] == token0 ? (uint256(reserve0), uint256(reserve1)) : (uint256(reserve1), uint256(reserve0));
            amounts[i + 1] = CPMMMath.getAmountOut(amounts[i], reserveIn, reserveOut);
        }
    }

    function _sortTokens(address tokenA, address tokenB) private pure returns (address token0, address token1) {
        if (tokenA == tokenB) revert InvalidPath();
        if (tokenA == address(0) || tokenB == address(0)) revert ZeroAddress();
        (token0, token1) = tokenA < tokenB ? (tokenA, tokenB) : (tokenB, tokenA);
    }

    /// @dev Pull `value` straight from the user into the pair and assert the
    ///      pair's balance increased by exactly `value`, rejecting
    ///      fee-on-transfer tokens at the router boundary.
    function _pull(address token, address from, address pair, uint256 value) private {
        uint256 before = _balanceOf(token, pair);
        if (!IERC20Meta(token).transferFrom(from, pair, value)) revert TransferFailed();
        uint256 afterBal = _balanceOf(token, pair);
        if (afterBal - before != value) revert FeeOnTransferDetected();
    }

    function _balanceOf(address token, address account) private view returns (uint256) {
        (bool ok, bytes memory ret) = token.staticcall(abi.encodeWithSignature("balanceOf(address)", account));
        if (!ok || ret.length < 32) revert TransferFailed();
        return abi.decode(ret, (uint256));
    }
}
