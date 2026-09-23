// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

/// @title Integer math for the constant-product market maker.
/// @notice Every division floors (EVM semantics). All formulas follow the
///         canonical x*y=k model with a 30 bps fee (Uniswap-V2-style).
library CPMMMath {
    /// @notice Output amount for a swap.
    /// @dev amountOut = (amountIn * 997 * reserveOut) /
    ///                  (reserveIn * 1000 + amountIn * 997)   (floor)
    function getAmountOut(uint256 amountIn, uint256 reserveIn, uint256 reserveOut)
        internal
        pure
        returns (uint256 amountOut)
    {
        require(amountIn > 0, "CPMM: INSUFFICIENT_INPUT");
        require(reserveIn > 0 && reserveOut > 0, "CPMM: INSUFFICIENT_LIQUIDITY");
        uint256 amountInWithFee = amountIn * 997;
        uint256 numerator = amountInWithFee * reserveOut;
        uint256 denominator = reserveIn * 1000 + amountInWithFee;
        amountOut = numerator / denominator;
    }

    /// @notice LP shares minted for a non-first deposit.
    /// @dev min of the two ratios, floor.
    function mintedLiquidity(uint256 amountA, uint256 amountB, uint256 reserveA, uint256 reserveB, uint256 totalSupply)
        internal
        pure
        returns (uint256)
    {
        uint256 liquidityA = (amountA * totalSupply) / reserveA;
        uint256 liquidityB = (amountB * totalSupply) / reserveB;
        return liquidityA < liquidityB ? liquidityA : liquidityB;
    }

    /// @notice Token amounts returned when burning LP shares. Floor.
    function burnedAmounts(uint256 liquidity, uint256 totalSupply, uint256 reserveA, uint256 reserveB)
        internal
        pure
        returns (uint256 amountA, uint256 amountB)
    {
        amountA = (liquidity * reserveA) / totalSupply;
        amountB = (liquidity * reserveB) / totalSupply;
    }

    /// @notice Quote B needed to deposit alongside `amountA` at current ratio.
    function quote(uint256 amountA, uint256 reserveA, uint256 reserveB) internal pure returns (uint256 amountB) {
        require(amountA > 0, "CPMM: INSUFFICIENT_INPUT");
        require(reserveA > 0 && reserveB > 0, "CPMM: INSUFFICIENT_LIQUIDITY");
        amountB = (amountA * reserveB) / reserveA;
    }

    /// @notice Floor square root (Babylonian method).
    function sqrt(uint256 y) internal pure returns (uint256 z) {
        if (y > 3) {
            z = y;
            uint256 x = y / 2 + 1;
            while (x < z) {
                z = x;
                x = (y / x + x) / 2;
            }
        } else if (y != 0) {
            z = 1;
        }
    }
}
