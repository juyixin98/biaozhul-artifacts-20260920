// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title U512
/// @notice Unsigned 512-bit integer arithmetic implemented as four 128-bit limbs
///         (little-endian, base B = 2^128), operating entirely in uint256 registers.
/// @dev    Fee settlement multiplies a uint256 principal by a per-block rate
///         (up to 2^64) and by a block count. The exact product can reach ~512
///         bits, so 256-bit registers are not enough; truncation here would be
///         the exact "lost dust" bug this project exists to prevent.
///
///         Invariant of every `U`: each limb is strictly smaller than B = 2^128.
library U512 {
    uint256 private constant BASE = 1 << 128;
    uint256 private constant MASK = BASE - 1;
    uint256 private constant LO64 = (1 << 64) - 1;

    struct U {
        uint256 a0;
        uint256 a1;
        uint256 a2;
        uint256 a3;
    }

    function zero() internal pure returns (U memory r) {}

    function fromU256(uint256 x) internal pure returns (U memory r) {
        r.a0 = x & MASK;
        r.a1 = x >> 128;
    }

    function isZero(U memory x) internal pure returns (bool) {
        return x.a0 == 0 && x.a1 == 0 && x.a2 == 0 && x.a3 == 0;
    }

    function eq(U memory x, U memory y) internal pure returns (bool) {
        return x.a0 == y.a0 && x.a1 == y.a1 && x.a2 == y.a2 && x.a3 == y.a3;
    }

    /// @notice Exact 256x256 -> 512 multiplication.
    /// @dev Schoolbook convolution over base B, with carries propagated after
    ///      every addition. Bounds (all proven with B = 2^128, every limb < B):
    ///        - product p <= (B-1)^2 = B^2 - 2B + 1
    ///        - adding a carry/limb c < B:  p + c <= B^2 - B < 2^256  (fits uint256)
    ///        - column 2 carry <= 2B-2, so x1*y1 + carry <= B^2 - 1  (fits exactly)
    function mul256(uint256 x, uint256 y) internal pure returns (U memory r) {
        uint256 x0 = x & MASK;
        uint256 x1 = x >> 128;
        uint256 y0 = y & MASK;
        uint256 y1 = y >> 128;

        // column 0: x0*y0
        uint256 t = x0 * y0;
        r.a0 = t & MASK;
        uint256 carry = t >> 128; // < B

        // column 1: x0*y1 + x1*y0 + carry0  (accumulated in two bounded steps)
        t = x0 * y1 + carry; // <= B^2 - B
        uint256 lo = t & MASK;
        carry = t >> 128; // < B
        t = x1 * y0 + lo; // <= B^2 - B
        r.a1 = t & MASK;
        carry += t >> 128; // <= 2B - 2

        // column 2: x1*y1 + carry <= B^2 - 1 (fits a uint256 exactly)
        t = x1 * y1 + carry;
        r.a2 = t & MASK;
        r.a3 = t >> 128; // < B
    }

    /// @notice Multiply a 512-bit value by a 256-bit scalar, reverting if the
    ///         true result exceeds 512 bits.
    /// @dev 8 limb products (i in 0..3, j in 0..1) accumulated into 6 base-B
    ///      columns via `_addProd`, which keeps every register < 2^256.
    function mulSmall(U memory m, uint256 v) internal pure returns (U memory r) {
        uint256[6] memory z;
        uint256 v0 = v & MASK;
        uint256 v1 = v >> 128;

        _addProd(z, 0, m.a0 * v0);
        _addProd(z, 1, m.a0 * v1);
        _addProd(z, 1, m.a1 * v0);
        _addProd(z, 2, m.a1 * v1);
        _addProd(z, 2, m.a2 * v0);
        _addProd(z, 3, m.a2 * v1);
        _addProd(z, 3, m.a3 * v0);
        _addProd(z, 4, m.a3 * v1);

        require(z[4] == 0 && z[5] == 0, "U512: overflow");
        r.a0 = z[0];
        r.a1 = z[1];
        r.a2 = z[2];
        r.a3 = z[3];
    }

    /// @dev Add one limb product p < B^2 into column k of z, propagating the
    ///      base-B carry upwards. z[k] < B on entry so z[k] + p <= B^2 - B < 2^256.
    function _addProd(uint256[6] memory z, uint256 k, uint256 p) private pure {
        uint256 t = z[k] + p;
        z[k] = t & MASK;
        uint256 carry = t >> 128; // < B
        unchecked {
            for (uint256 j = k + 1; carry != 0 && j < 6; ++j) {
                t = z[j] + carry; // <= 2B - 2
                z[j] = t & MASK;
                carry = t >> 128; // 0 or 1
            }
        }
        require(carry == 0, "U512: overflow");
    }

    /// @notice Add a 256-bit (or smaller) value to a 512-bit value in place.
    function addU256(U memory x, uint256 v) internal pure {
        uint256 t = x.a0 + (v & MASK); // <= 2B - 2
        x.a0 = t & MASK;
        uint256 carry = t >> 128;
        v >>= 128;
        if (carry == 0 && v == 0) return;

        t = x.a1 + (v & MASK) + carry; // <= 2B - 1
        x.a1 = t & MASK;
        carry = t >> 128;
        v >>= 128;
        if (carry == 0 && v == 0) return;

        t = x.a2 + (v & MASK) + carry;
        x.a2 = t & MASK;
        carry = t >> 128;
        v >>= 128;
        if (carry == 0 && v == 0) return;

        t = x.a3 + (v & MASK) + carry;
        x.a3 = t & MASK;
        carry = t >> 128;
        v >>= 128;
        require(carry == 0 && v == 0, "U512: overflow");
    }

    /// @notice x += y in place, reverting on 512-bit overflow.
    function addEq(U memory x, U memory y) internal pure {
        uint256 t = x.a0 + y.a0;
        x.a0 = t & MASK;
        uint256 carry = t >> 128;
        t = x.a1 + y.a1 + carry;
        x.a1 = t & MASK;
        carry = t >> 128;
        t = x.a2 + y.a2 + carry;
        x.a2 = t & MASK;
        carry = t >> 128;
        t = x.a3 + y.a3 + carry;
        x.a3 = t & MASK;
        require(t >> 128 == 0, "U512: overflow");
    }

    /// @notice Exact division of a 512-bit value by the fee scale S = 2^64.
    /// @return q quotient (floor(x / S)), up to 448 bits
    /// @return r remainder x mod S, i.e. the carried fractional fee, < 2^64
    /// @dev S is a power of two, so this is a 64-bit limb shift across the
    ///      128-bit limbs (no long division, constant gas).
    function divByScale(U memory x) internal pure returns (U memory q, uint256 r) {
        r = x.a0 & LO64;
        q.a0 = (x.a0 >> 64) | ((x.a1 & LO64) << 64);
        q.a1 = (x.a1 >> 64) | ((x.a2 & LO64) << 64);
        q.a2 = (x.a2 >> 64) | ((x.a3 & LO64) << 64);
        q.a3 = x.a3 >> 64;
    }
}
