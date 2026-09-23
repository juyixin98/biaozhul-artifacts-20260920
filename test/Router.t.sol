// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {Test} from "forge-std/Test.sol";
import {CPMMFactory} from "../src/CPMMFactory.sol";
import {CPMMPair} from "../src/CPMMPair.sol";
import {CPMMRouter} from "../src/CPMMRouter.sol";
import {CPMMMath} from "../src/CPMMMath.sol";
import {TestERC20} from "../src/mocks/TestERC20.sol";

contract RouterTest is Test {
    CPMMFactory internal factory;
    CPMMRouter internal router;
    TestERC20 internal tA;
    TestERC20 internal tB;
    TestERC20 internal tC;

    address internal alice = address(0xA11CE);
    address internal bob = address(0xB0B);

    function setUp() public {
        factory = new CPMMFactory();
        router = new CPMMRouter(address(factory));
        tA = new TestERC20("A", "A");
        tB = new TestERC20("B", "B");
        tC = new TestERC20("C", "C");
        for (uint8 i; i < 3; ++i) {
            tA.mint(alice, 1_000_000 ether);
            tB.mint(alice, 1_000_000 ether);
            tC.mint(alice, 1_000_000 ether);
        }
        tA.mint(bob, 1_000_000 ether);
        tB.mint(bob, 1_000_000 ether);
        tC.mint(bob, 1_000_000 ether);
    }

    function _seedPair(TestERC20 x, TestERC20 y, uint256 ax, uint256 ay) internal {
        address pair = factory.getPair(address(x), address(y));
        if (pair == address(0)) pair = factory.createPair(address(x), address(y));
        vm.startPrank(alice);
        x.approve(address(router), type(uint256).max);
        y.approve(address(router), type(uint256).max);
        router.addLiquidity(address(x), address(y), ax, ay, 0, 0, alice, block.timestamp + 1);
        vm.stopPrank();
    }

    // ------------------------------------------------------------------
    // Add / remove liquidity
    // ------------------------------------------------------------------

    function test_AddLiquidity_CreatesPairAndMintsProportionally() public {
        vm.startPrank(alice);
        tA.approve(address(router), type(uint256).max);
        tB.approve(address(router), type(uint256).max);
        (uint256 usedA, uint256 usedB, uint256 liq) =
            router.addLiquidity(address(tA), address(tB), 500 ether, 500 ether, 0, 0, alice, block.timestamp + 1);
        vm.stopPrank();
        assertEq(usedA, 500 ether);
        assertEq(usedB, 500 ether);
        assertEq(liq, 500 ether - 1_000);
    }

    function test_AddLiquidity_RejectsExpiredDeadline() public {
        vm.warp(1_000);
        vm.startPrank(alice);
        tA.approve(address(router), type(uint256).max);
        tB.approve(address(router), type(uint256).max);
        vm.expectRevert(CPMMRouter.Expired.selector);
        router.addLiquidity(address(tA), address(tB), 100 ether, 100 ether, 0, 0, alice, block.timestamp - 1);
        vm.stopPrank();
    }

    function test_AddLiquidity_RejectsSlippage() public {
        _seedPair(tA, tB, 1_000 ether, 1_000 ether);
        // Desired 100 A but only 50 B; the optimal B for 100 A is 100. The
        // router falls back to 50 B -> 50 A. Require >= 60 A -> slippage.
        vm.startPrank(alice);
        tA.approve(address(router), type(uint256).max);
        tB.approve(address(router), type(uint256).max);
        vm.expectRevert(CPMMRouter.Slippage.selector);
        router.addLiquidity(
            address(tA), address(tB), 100 ether, 50 ether, 60 ether, 50 ether, alice, block.timestamp + 1
        );
        vm.stopPrank();
    }

    function test_RemoveLiquidity_ProRata() public {
        _seedPair(tA, tB, 400 ether, 900 ether);
        address pair = factory.getPair(address(tA), address(tB));
        uint256 shares = CPMMPair(pair).balanceOf(alice);

        uint256 beforeA = tA.balanceOf(alice);
        uint256 beforeB = tB.balanceOf(alice);
        vm.startPrank(alice);
        CPMMPair(pair).approve(address(router), type(uint256).max);
        (uint256 gotA, uint256 gotB) =
            router.removeLiquidity(address(tA), address(tB), shares, 0, 0, alice, block.timestamp + 1);
        vm.stopPrank();
        assertEq(tA.balanceOf(alice) - beforeA, gotA);
        assertEq(tB.balanceOf(alice) - beforeB, gotB);
        // Expected floor amounts.
        uint256 total = CPMMPair(pair).totalSupply();
        // Reserves before burn were 400/900; burn used shares/(shares+1000).
        // gotA must be below 400e18 because the locked 1_000 shares remain.
        assertLt(gotA, 400 ether);
        assertLt(gotB, 900 ether);
        assertEq(CPMMPair(pair).balanceOf(alice), 0);
        assertEq(CPMMPair(pair).balanceOf(address(0)), 1_000);
        total;
    }

    function test_RemoveLiquidity_RejectsSlippageMin() public {
        _seedPair(tA, tB, 400 ether, 900 ether);
        address pair = factory.getPair(address(tA), address(tB));
        uint256 shares = CPMMPair(pair).balanceOf(alice);
        vm.startPrank(alice);
        CPMMPair(pair).approve(address(router), type(uint256).max);
        vm.expectRevert(CPMMRouter.Slippage.selector);
        router.removeLiquidity(address(tA), address(tB), shares, type(uint256).max, 0, alice, block.timestamp + 1);
        vm.stopPrank();
    }

    function test_RemoveLiquidity_RejectsExpired() public {
        _seedPair(tA, tB, 400 ether, 900 ether);
        address pair = factory.getPair(address(tA), address(tB));
        uint256 shares = CPMMPair(pair).balanceOf(alice);
        vm.warp(5_000);
        vm.startPrank(alice);
        CPMMPair(pair).approve(address(router), type(uint256).max);
        vm.expectRevert(CPMMRouter.Expired.selector);
        router.removeLiquidity(address(tA), address(tB), shares, 0, 0, alice, block.timestamp - 1);
        vm.stopPrank();
    }

    // ------------------------------------------------------------------
    // Swap
    // ------------------------------------------------------------------

    function test_SwapExactTokens_HonorsAmountOutMin() public {
        _seedPair(tA, tB, 1_000 ether, 1_000 ether);
        address[] memory path = new address[](2);
        path[0] = address(tA);
        path[1] = address(tB);
        uint256 expected = router.getAmountOut(10 ether, address(tA), address(tB));
        vm.startPrank(bob);
        tA.approve(address(router), type(uint256).max);
        uint256[] memory amounts = router.swapExactTokensForTokens(10 ether, expected, path, bob, block.timestamp + 1);
        vm.stopPrank();
        assertEq(amounts[0], 10 ether);
        assertEq(amounts[1], expected);
        assertEq(tB.balanceOf(bob), 1_000_000 ether + expected);
    }

    function test_Swap_RevertsWhenAmountOutMinTooHigh() public {
        _seedPair(tA, tB, 1_000 ether, 1_000 ether);
        address[] memory path = new address[](2);
        path[0] = address(tA);
        path[1] = address(tB);
        vm.startPrank(bob);
        tA.approve(address(router), type(uint256).max);
        vm.expectRevert(CPMMRouter.Slippage.selector);
        router.swapExactTokensForTokens(10 ether, type(uint256).max, path, bob, block.timestamp + 1);
        vm.stopPrank();
    }

    function test_Swap_RevertsAfterDeadline() public {
        _seedPair(tA, tB, 1_000 ether, 1_000 ether);
        address[] memory path = new address[](2);
        path[0] = address(tA);
        path[1] = address(tB);
        vm.warp(7_777);
        vm.startPrank(bob);
        tA.approve(address(router), type(uint256).max);
        vm.expectRevert(CPMMRouter.Expired.selector);
        router.swapExactTokensForTokens(10 ether, 0, path, bob, block.timestamp - 1);
        vm.stopPrank();
    }

    function test_Swap_MultiHop_ChainsReserves() public {
        _seedPair(tA, tB, 1_000 ether, 1_000 ether);
        _seedPair(tB, tC, 500 ether, 2_000 ether);
        address[] memory path = new address[](3);
        path[0] = address(tA);
        path[1] = address(tB);
        path[2] = address(tC);

        // Independent expected amounts.
        address ab = factory.getPair(address(tA), address(tB));
        address bc = factory.getPair(address(tB), address(tC));
        (uint112 ab0, uint112 ab1,) = CPMMPair(ab).getReserves();
        (uint256 rinAB, uint256 routAB) =
            address(tA) < address(tB) ? (uint256(ab0), uint256(ab1)) : (uint256(ab1), uint256(ab0));
        uint256 mid = CPMMMath.getAmountOut(10 ether, rinAB, routAB);
        (uint112 bc0, uint112 bc1,) = CPMMPair(bc).getReserves();
        (uint256 rinBC, uint256 routBC) =
            address(tB) < address(tC) ? (uint256(bc0), uint256(bc1)) : (uint256(bc1), uint256(bc0));
        uint256 out = CPMMMath.getAmountOut(mid, rinBC, routBC);

        vm.startPrank(bob);
        tA.approve(address(router), type(uint256).max);
        uint256[] memory amounts = router.swapExactTokensForTokens(10 ether, 0, path, bob, block.timestamp + 1);
        vm.stopPrank();
        assertEq(amounts[1], mid, "hop1");
        assertEq(amounts[2], out, "hop2");
        assertEq(tC.balanceOf(bob), 1_000_000 ether + out);
        // Intermediate B balance never sits with the router (stateless).
        assertEq(tB.balanceOf(address(router)), 0);
    }

    function test_Swap_FailedHopRollsBackAtomically() public {
        _seedPair(tA, tB, 1_000 ether, 1_000 ether);
        // tB/tC pair does not exist -> router reverts mid-multihop. No state
        // from the first hop should survive.
        address[] memory path = new address[](3);
        path[0] = address(tA);
        path[1] = address(tB);
        path[2] = address(tC);
        uint256 balABefore = tA.balanceOf(bob);
        (uint112 r0, uint112 r1,) = CPMMPair(factory.getPair(address(tA), address(tB))).getReserves();
        vm.startPrank(bob);
        tA.approve(address(router), type(uint256).max);
        // Pair lookup for B/C returns address(0); getReserves on zero address
        // returns 0 -> getAmountOut reverts INSUFFICIENT_LIQUIDITY.
        vm.expectRevert();
        router.swapExactTokensForTokens(10 ether, 0, path, bob, block.timestamp + 1);
        vm.stopPrank();
        assertEq(tA.balanceOf(bob), balABefore, "input refunded");
        (uint112 r0b, uint112 r1b,) = CPMMPair(factory.getPair(address(tA), address(tB))).getReserves();
        assertEq(uint256(r0b), uint256(r0), "reserve0 unchanged");
        assertEq(uint256(r1b), uint256(r1), "reserve1 unchanged");
    }

    function test_Swap_InvalidPathReverts() public {
        address[] memory tooShort = new address[](1);
        tooShort[0] = address(tA);
        vm.prank(bob);
        vm.expectRevert(CPMMRouter.InvalidPath.selector);
        router.swapExactTokensForTokens(1, 0, tooShort, bob, block.timestamp + 1);
    }
}
