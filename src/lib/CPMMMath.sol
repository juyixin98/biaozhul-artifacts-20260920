// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

/// @title CPMMMath
/// @notice Pure integer math for the constant-product pool.
/// @dev Every division is an explicit floor (EVM semantics). Rounding always
///      favours the pool / remaining LPs:
///        - input fee is taken on the input:  inAfterFee = in * 997 / 1000 (floor)
///        - output: out = inAfterFee * rOut / (rIn + inAfterFee) (floor)
///        - minted shares use a floored min() of the two proportional claims
///        - burned shares pay floored amounts, leaving dust in the reserves
///      This library reverts nothing: the caller validates inputs and zero
///      outputs so that all error selectors live in the pool contract.
library CPMMMath {
    /// @dev 0.30 % fee: swapper spends 1000, effectively trades with 997.
    uint256 internal constant FEE_NUMERATOR = 997;
    uint256 internal constant FEE_DENOMINATOR = 1000;

    /// @dev First MINIMUM_LIQUIDITY shares are minted to address(0) and locked.
    uint256 internal constant MINIMUM_LIQUIDITY = 1000;

    /// @notice Floor square root (Babylonian method), exact for all uint256.
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

    /// @notice Output amount for a swap. Returns 0 for dust inputs; the pool
    ///         rejects zero outputs so a tiny swap can never bypass the fee.
    ///         out = floor( floor(in*997/1000) * rOut / (rIn + floor(in*997/1000)) )
    function calcAmountOut(uint256 amountIn, uint256 reserveIn, uint256 reserveOut) internal pure returns (uint256) {
        uint256 inAfterFee = (amountIn * FEE_NUMERATOR) / FEE_DENOMINATOR;
        if (inAfterFee == 0) return 0;
        return (inAfterFee * reserveOut) / (reserveIn + inAfterFee);
    }

    /// @notice Shares minted for a deposit.
    ///         First deposit: floor(sqrt(a0*a1)) - MINIMUM_LIQUIDITY (caller
    ///         checks the result is positive).
    ///         Later deposits: min(floor(a0*S/r0), floor(a1*S/r1)).
    function calcMintShares(uint256 amount0, uint256 amount1, uint256 r0, uint256 r1, uint256 totalShares)
        internal
        pure
        returns (uint256 shares, bool firstDeposit)
    {
        if (totalShares == 0) {
            firstDeposit = true;
            uint256 raw = sqrt(amount0 * amount1);
            shares = raw > MINIMUM_LIQUIDITY ? raw - MINIMUM_LIQUIDITY : 0;
        } else {
            uint256 s0 = (amount0 * totalShares) / r0;
            uint256 s1 = (amount1 * totalShares) / r1;
            shares = s0 < s1 ? s0 : s1;
        }
    }

    /// @notice Token amounts returned for burning shares: floors on both sides.
    function calcBurnAmounts(uint256 sharesBurned, uint256 r0, uint256 r1, uint256 totalShares)
        internal
        pure
        returns (uint256 amount0, uint256 amount1)
    {
        amount0 = (sharesBurned * r0) / totalShares;
        amount1 = (sharesBurned * r1) / totalShares;
    }
}
