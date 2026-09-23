// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {Test} from "forge-std/Test.sol";
import {CPMMFactory} from "../src/CPMMFactory.sol";
import {CPMMPair} from "../src/CPMMPair.sol";
import {TestERC20} from "../src/mocks/TestERC20.sol";
import {ReentrantERC20} from "../src/mocks/ReentrantERC20.sol";
import {ReentrantCallee} from "../src/mocks/ReentrantCallee.sol";

/// @notice Every state-changing entry point is guarded by a single lock.
///         Reentry from a malicious token (during transfer) and from a flash
///         callback must both be blocked, and failed calls roll back atomically.
contract ReentrancyTest is Test {
    CPMMFactory internal factory;
    TestERC20 internal good;
    ReentrantERC20 internal evil;
    CPMMPair internal pair;

    address internal alice = address(0xA11CE);

    function setUp() public {
        factory = new CPMMFactory();
        good = new TestERC20("Good", "GOOD");
        evil = new ReentrantERC20();
        pair = CPMMPair(factory.createPair(address(good), address(evil)));
        good.mint(alice, 1_000_000 ether);
        evil.mint(alice, 1_000_000 ether);
        evil.configure(address(pair), 0);
        // Bootstrap with attacks enabled: reentries must be swallowed.
        vm.startPrank(alice);
        good.transfer(address(pair), 1_000 ether);
        evil.transfer(address(pair), 1_000 ether);
        (uint256 e0, uint256 e1) =
            address(good) < address(evil) ? (1_000 ether, 1_000 ether) : (1_000 ether, 1_000 ether);
        pair.mint(alice, e0, e1);
        vm.stopPrank();
    }

    function test_Reentrancy_BlockedDuringSwap() public {
        evil.configure(address(pair), 0);
        uint256 out = _quoteSwapOut(10 ether);
        vm.startPrank(alice);
        evil.transfer(address(pair), 10 ether);
        // The malicious token attempts p.swap(...) inside its transfer hook.
        bool isEvil0 = address(evil) == address(pair.token0());
        if (isEvil0) pair.swap(0, out, alice, 10 ether, 0, "");
        else pair.swap(out, 0, alice, 0, 10 ether, "");
        vm.stopPrank();
        // Outer swap still succeeded; reserves consistent.
        _assertReservesEqBalances();
    }

    function test_Reentrancy_BlockedDuringMint() public {
        evil.configure(address(pair), 1);
        vm.startPrank(alice);
        good.transfer(address(pair), 10 ether);
        evil.transfer(address(pair), 10 ether);
        (uint256 e0, uint256 e1) = address(good) < address(evil) ? (10 ether, 10 ether) : (10 ether, 10 ether);
        pair.mint(alice, e0, e1);
        vm.stopPrank();
        _assertReservesEqBalances();
    }

    function test_Reentrancy_BlockedDuringBurn() public {
        evil.configure(address(pair), 2);
        uint256 shares = pair.balanceOf(alice) / 4;
        vm.startPrank(alice);
        pair.transfer(address(pair), shares);
        pair.burn(alice);
        vm.stopPrank();
        _assertReservesEqBalances();
    }

    function test_Reentrancy_BlockedDuringSkimAndSync() public {
        evil.configure(address(pair), 3);
        // Donate evil tokens, skim triggers a transfer -> reentry attempt.
        vm.prank(alice);
        evil.transfer(address(pair), 1 ether);
        pair.skim(alice);

        evil.configure(address(pair), 4);
        vm.prank(alice);
        evil.transfer(address(pair), 1 ether);
        pair.sync();
        _assertReservesEqBalances();
    }

    function test_FlashCallback_ReentryBlockedAndNonRepaymentReverts() public {
        // A flash swap to the malicious callee. It tries to reenter (blocked)
        // and repays nothing, so the call reverts with InsufficientInputAmount
        // and the whole transaction rolls back atomically.
        ReentrantCallee attacker = new ReentrantCallee(address(pair)); // mode 0
        bool isGood0 = address(good) == address(pair.token0());
        uint256 out = 1 ether;
        vm.expectRevert(CPMMPair.InsufficientInputAmount.selector);
        if (isGood0) pair.swap(0, out, address(attacker), 0, 0, hex"01");
        else pair.swap(out, 0, address(attacker), 0, 0, hex"01");
        _assertReservesEqBalances();
    }

    function test_FlashCallback_PartialRepaymentFailsK() public {
        // Repays principal but not the fee: an input exists but k decreases.
        ReentrantCallee attacker = new ReentrantCallee(address(pair));
        attacker.setMode(1);
        vm.prank(alice);
        good.transfer(address(attacker), 1_000 ether);
        vm.prank(alice);
        evil.transfer(address(attacker), 1_000 ether);

        bool isGood0 = address(good) == address(pair.token0());
        uint256 out = 1 ether;
        vm.expectRevert(CPMMPair.KInvariant.selector);
        if (isGood0) pair.swap(0, out, address(attacker), 0, out, hex"01");
        else pair.swap(out, 0, address(attacker), out, 0, hex"01");
        _assertReservesEqBalances();
    }

    function test_FlashCallback_HonestRepaymentSucceeds() public {
        // Fund the callee so it can repay the flash output plus fee.
        ReentrantCallee callee = new ReentrantCallee(address(pair));
        callee.setMode(2);
        vm.prank(alice);
        good.transfer(address(callee), 100 ether);
        vm.prank(alice);
        evil.transfer(address(callee), 100 ether);

        bool isGood0 = address(good) == address(pair.token0());
        uint256 out = 5 ether;
        uint256 required = callee.requiredInput(out);

        uint256 r0Before;
        uint256 r1Before;
        {
            (uint112 r0, uint112 r1,) = pair.getReserves();
            r0Before = r0;
            r1Before = r1;
        }
        // Declared inputs must match the realized repayment exactly.
        if (isGood0) pair.swap(0, out, address(callee), 0, required, hex"01");
        else pair.swap(out, 0, address(callee), required, 0, hex"01");

        // k grows (fee retained).
        (uint112 r0, uint112 r1,) = pair.getReserves();
        assertGe(uint256(r0) * r1, r0Before * r1Before);
        _assertReservesEqBalances();
    }

    function _quoteSwapOut(uint256 amountIn) internal view returns (uint256) {
        (uint112 r0, uint112 r1,) = pair.getReserves();
        bool isEvil0 = address(evil) == address(pair.token0());
        uint256 rin = isEvil0 ? uint256(r0) : uint256(r1);
        uint256 rout = isEvil0 ? uint256(r1) : uint256(r0);
        return (amountIn * 997 * rout) / (rin * 1000 + amountIn * 997);
    }

    function _assertReservesEqBalances() internal view {
        (uint112 r0, uint112 r1,) = pair.getReserves();
        assertEq(TestERC20(pair.token0()).balanceOf(address(pair)), uint256(r0));
        assertEq(TestERC20(pair.token1()).balanceOf(address(pair)), uint256(r1));
    }
}
