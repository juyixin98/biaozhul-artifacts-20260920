// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

/// @title Math512
/// @notice floor(x*y/d) with a full 512-bit intermediate product. Pure integer
///         arithmetic; no rounding shortcuts. Based on Remco Bloemen's
///         512-bit division algorithm (MIT-licensed implementations in Solmate
///         and 0xSequence).
library Math512 {
    error DivisionByZero();
    error MulDivOverflow();

    function mulDivDown(uint256 x, uint256 y, uint256 d) internal pure returns (uint256 result) {
        if (d == 0) revert DivisionByZero();

        // Full 256x256 -> 512 bit product: (h, l) = x * y.
        uint256 l;
        uint256 h;
        assembly {
            let mm := mulmod(x, y, not(0))
            l := mul(x, y)
            h := sub(sub(mm, l), lt(mm, l))
        }

        // If h >= d the quotient does not fit in 256 bits.
        if (h >= d) revert MulDivOverflow();
        if (h == 0) return l / d;

        unchecked {
            // Make the 512-bit division exact: subtract the remainder
            // (x*y mod d) from [h l] so d divides the adjusted product.
            uint256 mm = mulmod(x, y, d);
            if (mm > l) h -= 1;
            l -= mm;

            uint256 pow2 = d & (0 - d);
            d /= pow2;
            l /= pow2;
            l += h * ((0 - pow2) / pow2 + 1);

            // Eight iterations of 256-bit reciprocal Newton iteration.
            uint256 r = 1;
            r *= 2 - d * r;
            r *= 2 - d * r;
            r *= 2 - d * r;
            r *= 2 - d * r;
            r *= 2 - d * r;
            r *= 2 - d * r;
            r *= 2 - d * r;
            r *= 2 - d * r;
            return l * r;
        }
    }
}
