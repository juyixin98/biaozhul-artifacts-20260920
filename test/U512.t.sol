// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {U512} from "../contracts/U512.sol";

/// @notice Minimal forge-std-free harness: forge's Test runner requires only
///         setUp(); the `Vm` cheatcode interface is called directly at its
///         well-known address.
interface Vm {
    function roll(uint256) external;
    function prank(address) external;
    function startPrank(address) external;
    function stopPrank() external;
    function expectRevert() external;
    function expectRevert(bytes calldata) external;
}

contract U512Test {
    using U512 for U512.U;

    Vm internal constant vm = Vm(0x7109709ECfa91a80626fF3989D68f67F5b1DD12D);

    function setUp() public {}

    function _eq(U512.U memory u, uint256 a0, uint256 a1, uint256 a2, uint256 a3)
        private
        pure
        returns (bool)
    {
        return u.a0 == a0 && u.a1 == a1 && u.a2 == a2 && u.a3 == a3;
    }

    // ---------- known vectors ----------

    function testZeroAndFromU256() public pure {
        U512.U memory z = U512.zero();
        assertTrue(_eq(z, 0, 0, 0, 0));
        assertTrue(z.isZero());

        U512.U memory one = U512.fromU256(1);
        assertTrue(_eq(one, 1, 0, 0, 0));

        U512.U memory top = U512.fromU256(type(uint256).max);
        assertTrue(_eq(top, (1 << 128) - 1, (1 << 128) - 1, 0, 0));

        U512.U memory pivot = U512.fromU256(1 << 128);
        assertTrue(_eq(pivot, 0, 1, 0, 0));
    }

    function testMul256KnownVectors() public pure {
        // 0 * x = 0
        assertTrue(_eq(U512.mul256(0, type(uint256).max), 0, 0, 0, 0));
        // 1 * max = max
        assertTrue(_eq(U512.mul256(1, type(uint256).max), (1 << 128) - 1, (1 << 128) - 1, 0, 0));
        // B * B = 2^256 -> limb a2 = 1
        assertTrue(_eq(U512.mul256(1 << 128, 1 << 128), 0, 0, 1, 0));
        // (2^128 - 1)^2 = 2^256 - 2^129 + 1 -> a0 = 1, a1 = 2^128 - 2
        assertTrue(_eq(U512.mul256((1 << 128) - 1, (1 << 128) - 1), 1, (1 << 128) - 2, 0, 0));
        // max256^2 = 2^512 - 2^257 + 1
        //   = 1 + (B-2)*B^2 + (B-1)*B^3   (B = 2^128)
        assertTrue(_eq(U512.mul256(type(uint256).max, type(uint256).max), 1, 0, (1 << 128) - 2, (1 << 128) - 1));
        // max * 1, via the 256x256 path, must equal fromU256(max)
        assertTrue(
            _eq(
                U512.mul256(type(uint256).max, 1),
                (1 << 128) - 1,
                (1 << 128) - 1,
                0,
                0
            )
        );
    }

    function testMulSmallKnownVectors() public pure {
        // 2^256 * (2^256 - 1) = 2^512 - 2^256 -> a2 = a3 = B-1, a0 = a1 = 0
        U512.U memory t = U512.mul256(1 << 128, 1 << 128); // 2^256 => a2=1
        t = U512.mulSmall(t, type(uint256).max);
        assertTrue(_eq(t, 0, 0, (1 << 128) - 1, (1 << 128) - 1));

        // max512 * 0 = 0
        U512.U memory big = U512.mul256(type(uint256).max, type(uint256).max);
        assertTrue(_eq(U512.mulSmall(big, 0), 0, 0, 0, 0));
        // max512 * 1 = max512
        assertTrue(_eq(U512.mulSmall(big, 1), 1, 0, (1 << 128) - 2, (1 << 128) - 1));
    }

    function testMulSmallOverflowReverts() public {
        U512Harness h = new U512Harness();
        // max512 * 2 exceeds 512 bits; the revert must be caught at an
        // external call boundary (internal library calls are inlined).
        U512.U memory max512 = U512.mul256(type(uint256).max, type(uint256).max);
        vm.expectRevert(bytes("U512: overflow"));
        h.mulSmallExternal(max512, 2);
    }

    function testAddEqBasic() public pure {
        U512.U memory a = U512.fromU256(type(uint256).max);
        U512.addEq(a, U512.fromU256(1)); // 2^256 -> a2 = 1
        assertTrue(_eq(a, 0, 0, 1, 0));
    }

    function testAddEqOverflowReverts() public {
        U512Harness h = new U512Harness();
        // true 512-bit maximum: all limbs = 2^128 - 1
        U512.U memory max512 = U512.U(type(uint128).max, type(uint128).max, type(uint128).max, type(uint128).max);
        vm.expectRevert(bytes("U512: overflow"));
        h.addEqExternal(max512, U512.fromU256(1));
    }

    function testAddU256OverflowReverts() public {
        U512Harness h = new U512Harness();
        U512.U memory max512 = U512.U(type(uint128).max, type(uint128).max, type(uint128).max, type(uint128).max);
        vm.expectRevert(bytes("U512: overflow"));
        h.addU256External(max512, 1);
    }

    function testDivByScaleKnownVectors() public pure {
        // max512 = 1 + (B-2)*B^2 + (B-1)*B^3, S = 2^64
        U512.U memory max512 = U512.mul256(type(uint256).max, type(uint256).max);
        (U512.U memory q, uint256 r) = U512.divByScale(max512);
        // q.a0 = (a0>>64) | ((a1 & (2^64-1))<<64) = 0
        assertEq(q.a0, 0);
        // q.a1 = (a1>>64) | ((a2 & (2^64-1))<<64) = (B-2 & (2^64-1))<<64
        //      = (2^64-2)<<64
        assertEq(q.a1, (uint256(1 << 64) - 2) << 64);
        // q.a2 = (a2>>64) | ((a3 & (2^64-1))<<64) = (2^64-1) | ((2^64-1)<<64) = B-1
        assertEq(q.a2, (1 << 128) - 1);
        // q.a3 = a3>>64 = (2^64-1)
        assertEq(q.a3, (1 << 64) - 1);
        // remainder is the low 64 bits of a0 = 1
        assertEq(r, 1);
    }

    // ---------- differential tests against EVM 256-bit arithmetic ----------
    // For values whose products fit in 256 bits, U512 must match `*` exactly.

    function testDiffMul256(uint128 x, uint128 y) public pure {
        U512.U memory u = U512.mul256(uint256(x), uint256(y));
        uint256 z = uint256(x) * uint256(y);
        assertEq(u.a0, z & ((1 << 128) - 1));
        assertEq(u.a1, z >> 128);
        assertEq(u.a2, 0);
        assertEq(u.a3, 0);
    }

    function testDiffMulSmall(uint128 x, uint128 y, uint64 k) public pure {
        // (x*y)*k can reach 2^320, so the reference is built as a direct
        // mul256(x, y*k) where y*k fits in 192 bits (<= 2^192 - 2^64 + 1).
        U512.U memory u = U512.mul256(uint256(x), uint256(y));
        u = U512.mulSmall(u, uint256(k));
        U512.U memory ref = U512.mul256(uint256(x), uint256(y) * uint256(k));
        assertTrue(u.eq(ref));
    }

    function testDiffAddU256(uint128 x, uint128 y, uint64 v) public pure {
        U512.U memory u = U512.fromU256(uint256(x) + uint256(y)); // <= 2^129
        uint256 ref = uint256(x) + uint256(y) + uint256(v);
        U512.addU256(u, v);
        assertEq(u.a0, ref & ((1 << 128) - 1));
        assertEq(u.a1, ref >> 128);
        assertEq(u.a2, 0);
        assertEq(u.a3, 0);
    }

    function testDiffAddEq(uint128 x, uint128 y) public pure {
        U512.U memory u = U512.fromU256(uint256(x));
        uint256 ref = uint256(x) + uint256(y);
        U512.addEq(u, U512.fromU256(uint256(y)));
        assertEq(u.a0, ref & ((1 << 128) - 1));
        assertEq(u.a1, ref >> 128);
        assertEq(u.a2, 0);
        assertEq(u.a3, 0);
    }

    function testDiffDivByScale(uint128 x, uint128 y) public pure {
        uint256 ref = uint256(x) * uint256(y); // <= 2^256
        U512.U memory u = U512.mul256(uint256(x), uint256(y));
        (U512.U memory q, uint256 r) = U512.divByScale(u);
        assertEq(q.a0, (ref >> 64) & ((1 << 128) - 1));
        assertEq(q.a1, ref >> 192);
        assertEq(q.a2, 0);
        assertEq(q.a3, 0);
        assertEq(r, ref & ((1 << 64) - 1));
        // q*2^64 + r == input
        assertTrue(q.a2 == 0 && q.a3 == 0);
        uint256 rebuilt = (q.a1 << 192) | (q.a0 << 64) | r;
        assertEq(rebuilt, ref);
    }

    // ---------- tiny assert helpers (no forge-std) ----------

    function assertTrue(bool cond) private pure {
        require(cond, "assertion failed");
    }

    function assertEq(uint256 a, uint256 b) private pure {
        require(a == b, "assertEq failed");
    }
}

/// @dev External-call boundary so vm.expectRevert can observe library reverts
///      (internal library calls are inlined into the caller otherwise).
contract U512Harness {
    function mulSmallExternal(U512.U memory x, uint256 v) external pure returns (U512.U memory) {
        return U512.mulSmall(x, v);
    }

    function addEqExternal(U512.U memory x, U512.U memory y) external pure returns (U512.U memory) {
        U512.addEq(x, y);
        return x;
    }

    function addU256External(U512.U memory x, uint256 v) external pure returns (U512.U memory) {
        U512.addU256(x, v);
        return x;
    }
}
