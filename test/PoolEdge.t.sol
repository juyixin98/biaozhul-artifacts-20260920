// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {Test} from "forge-std/Test.sol";
import {ConstantProductPool} from "../src/ConstantProductPool.sol";
import {TestERC20} from "../src/test/TestERC20.sol";
import {CPMMMath} from "../src/lib/CPMMMath.sol";

/// @title PoolEdgeTest
/// @notice Covers the remaining view/metadata functions and the uint112
///         reserve-overflow guard.
contract PoolEdgeTest is Test {
    uint256 internal constant W = 1e18;
    uint256 internal dl = block.timestamp + 1 days;
    address internal alice = makeAddr("alice");

    ConstantProductPool internal pool;
    TestERC20 internal t0;
    TestERC20 internal t1;

    function setUp() public {
        t0 = new TestERC20("Token0", "T0");
        t1 = new TestERC20("Token1", "T1");
        pool = new ConstantProductPool(address(t0), address(t1));
        t0.mint(alice, 2_000_000e18);
        t1.mint(alice, 2_000_000e18);
        vm.startPrank(alice);
        t0.approve(address(pool), type(uint256).max);
        t1.approve(address(pool), type(uint256).max);
        vm.stopPrank();
    }

    function test_Metadata_Views() public view {
        assertEq(pool.name(), "CPMM LP");
        assertEq(pool.symbol(), "CPLP");
        assertEq(pool.decimals(), 18);
        assertEq(pool.token0(), address(t0));
        assertEq(pool.token1(), address(t1));
        assertTrue(address(pool.probe()) != address(0));
        // empty-pool views
        (uint112 r0, uint112 r1, uint256 ts) = pool.getReserves();
        assertEq(uint256(r0), 0);
        assertEq(uint256(r1), 0);
        assertEq(ts, 0);
        assertEq(pool.totalSupply(), 0);
        assertEq(pool.balanceOf(alice), 0);
    }

    function test_Quote_InvalidToken_Reverts() public {
        try pool.quoteAmountOut(makeAddr("x"), 1) {
            revert("expected InvalidTokenIn");
        } catch (bytes memory d) {
            assertEq(bytes4(d), ConstantProductPool.InvalidTokenIn.selector);
        }
    }

    function test_PreviewMint_FirstAndLater() public {
        (uint256 sh, bool first) = pool.previewMintShares(1000e18, 1000e18);
        assertTrue(first);
        // raw geometric shares less the 1000 lock (mirrors calcMintShares)
        assertEq(sh, 1000e18 - 1000);

        vm.prank(alice);
        pool.addLiquidity(1000e18, 1000e18, 0, alice, dl);

        (sh, first) = pool.previewMintShares(10e18, 10e18);
        assertFalse(first);
        assertEq(sh, 10e18);

        // skewed preview uses the min side
        (sh,) = pool.previewMintShares(20e18, 10e18);
        assertEq(sh, 10e18);
    }

    function test_Quote_Token1Direction() public {
        vm.prank(alice);
        pool.addLiquidity(1000e18, 1000e18, 0, alice, dl);
        uint256 b = (5e18 * 997) / 1000;
        assertEq(pool.quoteAmountOut(address(t1), 5e18), (b * 1000e18) / (1000e18 + b));
    }

    /// @dev Reserves are uint112-packed; a deposit that would push a reserve
    ///      past 2^112-1 must revert atomically (no shares, no balances left
    ///      in the pool beyond what is consistent).
    function test_ReserveOverflow_RevertsAtomically() public {
        uint256 huge = uint256(type(uint112).max) + 1; // 2^112
        TestERC20 big = new TestERC20("BIG", "BIG");
        // second pool pairs the big token with t1
        ConstantProductPool p = new ConstantProductPool(address(big), address(t1));
        big.mint(alice, huge);
        vm.prank(alice);
        big.approve(address(p), type(uint256).max);
        vm.prank(alice);
        t1.approve(address(p), type(uint256).max);

        uint256 balT1Before = t1.balanceOf(alice);
        uint256 balBigBefore = big.balanceOf(alice);
        vm.prank(alice);
        vm.expectRevert(abi.encodeWithSelector(ConstantProductPool.ReservesDiverged.selector, address(big), huge, huge));
        p.addLiquidity(huge, 1000e18, 0, alice, dl);

        assertEq(p.totalSupply(), 0, "no shares minted");
        assertEq(big.balanceOf(alice), balBigBefore, "huge token returned");
        assertEq(t1.balanceOf(alice), balT1Before, "small token returned");
        assertEq(big.balanceOf(address(p)), 0);
        assertEq(t1.balanceOf(address(p)), 0);
    }

    function test_LpTransfer_InfiniteApproval_StaysInfinite() public {
        vm.prank(alice);
        (,, uint256 sh) = pool.addLiquidity(1000e18, 1000e18, 0, alice, dl);
        address bob = makeAddr("bob");
        vm.prank(alice);
        pool.approve(bob, type(uint256).max);
        vm.prank(bob);
        pool.transferFrom(alice, bob, sh - 1);
        assertEq(pool.allowance(alice, bob), type(uint256).max, "max stays max");
        assertEq(pool.balanceOf(bob), sh - 1);
    }

    function test_Math_LargeSqrt() public pure {
        // 2^200 and (2^200 - 1) boundary around an exact even power
        assertEq(CPMMMath.sqrt(2 ** 200), 2 ** 100);
        assertEq(CPMMMath.sqrt(2 ** 200 - 1), 2 ** 100 - 1);
        // amount-out formula handles reserves near the uint112 top
        uint256 r = type(uint112).max;
        uint256 out = CPMMMath.calcAmountOut(1e30, r, r);
        assertGt(out, 0);
        assertLt(out, 1e30);
    }
}
