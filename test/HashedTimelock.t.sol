// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {HashedTimelock} from "../src/HashedTimelock.sol";

contract HashedTimelockTest is Test {
    HashedTimelock internal htlc;

    address internal alice = makeAddr("alice");
    address internal bob = makeAddr("bob");

    bytes32 internal preimage = keccak256("demo-secret-preimage");
    bytes32 internal hashLock = keccak256(abi.encodePacked(preimage));
    uint256 internal amount = 1 ether;
    uint64 internal t0 = 1_000_000;
    uint64 internal timelock = t0 + 100;
    bytes32 internal swapId;

    event Claimed(bytes32 indexed swapId, bytes32 preimage, address indexed receiver);
    event Refunded(bytes32 indexed swapId, address indexed sender);

    function setUp() public {
        vm.warp(t0);
        htlc = new HashedTimelock();
        vm.deal(alice, 10 ether);

        vm.prank(alice);
        swapId = htlc.lock{value: amount}(hashLock, payable(bob), timelock);
    }

    function test_LockHoldsFunds() public view {
        assertEq(address(htlc).balance, amount);
        (, , , uint256 amt, uint64 tl, HashedTimelock.State st) = htlc.swaps(swapId);
        assertEq(amt, amount);
        assertEq(tl, timelock);
        assertEq(uint256(st), uint256(HashedTimelock.State.LOCKED));
    }

    function test_ClaimWithCorrectPreimage() public {
        vm.expectEmit(true, true, false, true);
        emit Claimed(swapId, preimage, bob);
        vm.prank(bob);
        htlc.claim(swapId, preimage);

        assertEq(bob.balance, amount);
        assertEq(uint256(htlc.getState(swapId)), uint256(HashedTimelock.State.CLAIMED));
        assertEq(address(htlc).balance, 0);
    }

    function test_RevertWhen_WrongPreimage() public {
        bytes32 bogus = keccak256("not-the-secret");
        vm.prank(bob);
        vm.expectRevert(abi.encodeWithSelector(HashedTimelock.WrongPreimage.selector, bogus, hashLock));
        htlc.claim(swapId, bogus);
        assertEq(uint256(htlc.getState(swapId)), uint256(HashedTimelock.State.LOCKED));
    }

    function test_RevertWhen_ClaimUnknownSwap() public {
        vm.expectRevert(abi.encodeWithSelector(HashedTimelock.UnknownSwap.selector, bytes32(uint256(1))));
        htlc.claim(bytes32(uint256(1)), preimage);
    }

    function test_RevertWhen_RefundBeforeDeadline() public {
        vm.warp(timelock - 1);
        vm.prank(alice);
        vm.expectRevert(abi.encodeWithSelector(HashedTimelock.TooEarly.selector, timelock - 1, timelock));
        htlc.refund(swapId);
    }

    function test_RefundExactlyAtDeadline() public {
        vm.warp(timelock);
        vm.expectEmit(true, true, false, true);
        emit Refunded(swapId, alice);
        vm.prank(alice);
        htlc.refund(swapId);
        assertEq(uint256(htlc.getState(swapId)), uint256(HashedTimelock.State.REFUNDED));
        assertEq(alice.balance, 10 ether);
    }

    function test_RevertWhen_RefundByNonSender() public {
        vm.warp(timelock);
        vm.prank(bob);
        vm.expectRevert(abi.encodeWithSelector(HashedTimelock.NotSender.selector, bob, alice));
        htlc.refund(swapId);
    }

    function test_RevertWhen_DoubleClaim() public {
        vm.prank(bob);
        htlc.claim(swapId, preimage);

        vm.prank(bob);
        vm.expectRevert(abi.encodeWithSelector(HashedTimelock.AlreadySettled.selector, HashedTimelock.State.CLAIMED));
        htlc.claim(swapId, preimage);
    }

    function test_RevertWhen_DoubleRefund() public {
        vm.warp(timelock + 10);
        vm.prank(alice);
        htlc.refund(swapId);

        vm.prank(alice);
        vm.expectRevert(abi.encodeWithSelector(HashedTimelock.AlreadySettled.selector, HashedTimelock.State.REFUNDED));
        htlc.refund(swapId);
    }

    function test_RevertWhen_RefundAfterClaim() public {
        vm.prank(bob);
        htlc.claim(swapId, preimage);
        vm.warp(timelock + 10);

        vm.prank(alice);
        vm.expectRevert(abi.encodeWithSelector(HashedTimelock.AlreadySettled.selector, HashedTimelock.State.CLAIMED));
        htlc.refund(swapId);
    }

    function test_RevertWhen_ClaimAfterRefund() public {
        vm.warp(timelock + 10);
        vm.prank(alice);
        htlc.refund(swapId);

        vm.prank(bob);
        vm.expectRevert(abi.encodeWithSelector(HashedTimelock.AlreadySettled.selector, HashedTimelock.State.REFUNDED));
        htlc.claim(swapId, preimage);
    }

    function test_RevertWhen_LockInPast() public {
        vm.prank(alice);
        vm.expectRevert(abi.encodeWithSelector(HashedTimelock.TimelockInPast.selector, t0, t0));
        htlc.lock{value: amount}(hashLock, payable(bob), t0);
    }

    function test_RevertWhen_LockZeroValue() public {
        vm.prank(alice);
        vm.expectRevert(HashedTimelock.ZeroAmount.selector);
        htlc.lock(hashLock, payable(bob), timelock);
    }

    /// @notice Claim is allowed past the deadline while still LOCKED: revealing the
    ///         preimage always wins. This is the documented HTLC boundary the two
    ///         legs' timeout gap (Delta) is meant to protect.
    function test_ClaimAfterDeadlineWhileLockedStillWins() public {
        vm.warp(timelock + 5);
        vm.prank(bob);
        htlc.claim(swapId, preimage);
        assertEq(bob.balance, amount);
        assertEq(uint256(htlc.getState(swapId)), uint256(HashedTimelock.State.CLAIMED));
    }
}
