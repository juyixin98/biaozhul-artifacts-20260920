// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {SyntheticToken} from "../src/SyntheticToken.sol";
import {LinearTokenVesting} from "../src/LinearTokenVesting.sol";

/// @notice End-to-end Foundry tests for the linear vesting escrow.
/// @dev Covers: start/end boundaries, cliff, duplicate claims, revocation /
///      claim interleaving (both orders), non-divisible total amounts, and
///      the final conservation identity:
///          released + refunded == initiallyLocked.
contract LinearTokenVestingTest is Test {
    SyntheticToken token;
    LinearTokenVesting vesting;

    address owner = address(this);
    address beneficiary = makeAddr("beneficiary");

    uint256 constant TOTAL = 1_000_000 ether;
    uint256 constant START = 1_000_000;
    uint256 constant CLIFF = 100;
    uint256 constant DURATION = 1_000;

    event Released(uint256 indexed id, address indexed beneficiary, uint256 amount);
    event Revoked(uint256 indexed id, uint256 vestedAtRevocation, uint256 refundedAmount);

    function setUp() public {
        vm.warp(START - 10);
        token = new SyntheticToken(TOTAL);
        vesting = new LinearTokenVesting(address(token));
        token.approve(address(vesting), type(uint256).max);
    }

    function _create(uint256 amount, bool revocable) internal returns (uint256 id) {
        id = vesting.createSchedule(beneficiary, amount, START, CLIFF, DURATION, revocable);
    }

    // ---------------------------------------------------------------------
    // Start / cliff / end boundaries
    // ---------------------------------------------------------------------

    function test_NothingVestedBeforeStartAndCliff() public {
        _create(TOTAL, false);
        vm.warp(START);
        assertEq(vesting.vestedAmount(0, START), 0, "at start (cliff not reached)");
        vm.warp(START + CLIFF - 1);
        assertEq(vesting.vestedAmount(0, START + CLIFF - 1), 0, "1s before cliff");

        vm.prank(beneficiary);
        vm.expectRevert(LinearTokenVesting.NothingToRelease.selector);
        vesting.release(0);
    }

    function test_VestsAtCliffBoundary() public {
        _create(TOTAL, false);
        vm.warp(START + CLIFF);
        // exactly CLIFF/DURATION = 10% at the cliff instant
        assertEq(vesting.vestedAmount(0, block.timestamp), TOTAL * CLIFF / DURATION);
    }

    function test_FullVestAtEndAndBeyond() public {
        _create(TOTAL, false);
        vm.warp(START + DURATION);
        assertEq(vesting.vestedAmount(0, START + DURATION), TOTAL, "at end");
        vm.warp(START + DURATION + 5_000);
        assertEq(vesting.vestedAmount(0, START + DURATION + 5_000), TOTAL, "past end");
    }

    function test_ReleaseAtEndPaysFullAmount() public {
        _create(TOTAL, false);
        vm.warp(START + DURATION);
        vm.prank(beneficiary);
        uint256 paid = vesting.release(0);
        assertEq(paid, TOTAL);
        assertEq(token.balanceOf(beneficiary), TOTAL);
        assertEq(token.balanceOf(address(vesting)), 0);
    }

    // ---------------------------------------------------------------------
    // Duplicate / repeated claims
    // ---------------------------------------------------------------------

    function test_RepeatedClaimsOnlyPayDelta() public {
        _create(TOTAL, false);
        uint256 lastBalance = 0;

        // claim at 25%, 50%, 75%, 100% of duration
        uint256[4] memory marks = [uint256(250), 500, 750, 1000];
        for (uint256 i = 0; i < marks.length; i++) {
            vm.warp(START + marks[i]);
            vm.prank(beneficiary);
            uint256 payment = vesting.release(0);
            uint256 balance = token.balanceOf(beneficiary);
            assertGt(payment, 0, "each checkpoint pays something");
            assertEq(balance - lastBalance, payment, "payment equals balance delta");
            assertEq(balance, TOTAL * marks[i] / DURATION, "cumulative vested matches curve");
            lastBalance = balance;
        }
        assertEq(lastBalance, TOTAL);

        // one more claim after full vesting must yield nothing
        vm.warp(START + DURATION + 100);
        vm.prank(beneficiary);
        vm.expectRevert(LinearTokenVesting.NothingToRelease.selector);
        vesting.release(0);
    }

    function test_DuplicateClaimInsideSameBlockPaysNothing() public {
        _create(TOTAL, false);
        vm.warp(START + DURATION / 2);
        vm.prank(beneficiary);
        vesting.release(0);
        uint256 balance = token.balanceOf(beneficiary);

        vm.prank(beneficiary);
        vm.expectRevert(LinearTokenVesting.NothingToRelease.selector);
        vesting.release(0);
        assertEq(token.balanceOf(beneficiary), balance);
    }

    // ---------------------------------------------------------------------
    // Non-divisible totals (rounding dust resolves at the end)
    // ---------------------------------------------------------------------

    function test_NonDivisibleTotalDustSettlesAtEnd() public {
        // 3 tokens (raw units) do not divide evenly over 1000 seconds.
        uint256 amount = 3;
        _create(amount, false);

        // at 500s only floor(3 * 500 / 1000) = 1 whole unit is vested
        vm.warp(START + DURATION / 2);
        vm.prank(beneficiary);
        uint256 first = vesting.release(0);
        assertEq(first, 1, "only whole units vest mid-way");

        vm.warp(START + DURATION);
        vm.prank(beneficiary);
        uint256 second = vesting.release(0);
        assertEq(second, 2, "remaining dust settles at the end");
        assertEq(first + second, amount, "released total equals locked total, no dust stuck");
        assertEq(token.balanceOf(address(vesting)), 0);
    }

    // ---------------------------------------------------------------------
    // Revocation interleaved with claims
    // ---------------------------------------------------------------------

    function test_RevokeBeforeAnyClaim_OrderA() public {
        _create(TOTAL, true);
        uint256 ownerBefore = token.balanceOf(owner);

        // revoke halfway
        vm.warp(START + DURATION / 2);
        vm.expectEmit(true, false, false, true);
        emit Revoked(0, TOTAL / 2, TOTAL - TOTAL / 2);
        vesting.revoke(0);

        // vested half stays for beneficiary; unvested half goes back immediately
        assertEq(token.balanceOf(owner), ownerBefore + (TOTAL - TOTAL / 2), "owner refunded");

        // time passes after revocation: vesting is frozen
        vm.warp(START + DURATION);
        assertEq(vesting.vestedAmount(0, block.timestamp), TOTAL / 2, "frozen after revoke");

        vm.prank(beneficiary);
        uint256 paid = vesting.release(0);
        assertEq(paid, TOTAL / 2, "beneficiary keeps vested portion");

        _assertConservation(0, TOTAL);
    }

    function test_ClaimThenRevokeThenClaim_OrderB() public {
        _create(TOTAL, true);
        uint256 ownerBefore = token.balanceOf(owner);

        // claim 25%
        vm.warp(START + DURATION / 4);
        vm.prank(beneficiary);
        vesting.release(0);
        assertEq(token.balanceOf(beneficiary), TOTAL / 4);

        // revoke at 60%
        vm.warp(START + DURATION * 6 / 10);
        uint256 vestedAtRevoke = TOTAL * 6 / 10;
        vesting.revoke(0);
        uint256 refund = TOTAL - vestedAtRevoke; // 40%
        assertEq(token.balanceOf(owner), ownerBefore + refund);

        // claim the remainder that vested between 25% and 60%
        vm.prank(beneficiary);
        uint256 secondClaim = vesting.release(0);
        assertEq(secondClaim, vestedAtRevoke - TOTAL / 4);

        // nothing more vests later
        vm.warp(START + DURATION + 999);
        vm.prank(beneficiary);
        vm.expectRevert(LinearTokenVesting.NothingToRelease.selector);
        vesting.release(0);

        _assertConservation(0, TOTAL);
    }

    function test_RevokeAfterFullVest_RefundsZero() public {
        _create(TOTAL, true);
        vm.warp(START + DURATION);
        vesting.revoke(0); // vested == total -> 0 refunded, no error
        vm.prank(beneficiary);
        assertEq(vesting.release(0), TOTAL);
        _assertConservation(0, TOTAL);
    }

    function test_RevokeDuringCliff_RefundsEverything() public {
        _create(TOTAL, true);
        uint256 ownerBefore = token.balanceOf(owner);
        vm.warp(START + CLIFF - 1);
        vesting.revoke(0);
        assertEq(token.balanceOf(owner), ownerBefore + TOTAL, "nothing vested yet");
        vm.prank(beneficiary);
        vm.expectRevert(LinearTokenVesting.NothingToRelease.selector);
        vesting.release(0);
        _assertConservation(0, TOTAL);
    }

    function test_CannotRevokeIrrevocableSchedule() public {
        _create(TOTAL, false);
        vm.warp(START + 1);
        vm.expectRevert(LinearTokenVesting.NotRevocable.selector);
        vesting.revoke(0);
    }

    function test_CannotRevokeTwice() public {
        _create(TOTAL, true);
        vm.warp(START + 100);
        vesting.revoke(0);
        vm.expectRevert(LinearTokenVesting.AlreadyRevoked.selector);
        vesting.revoke(0);
    }

    // ---------------------------------------------------------------------
    // Large / overflow-sensitive amounts
    // ---------------------------------------------------------------------

    function test_LargeAmountDoesNotOverflow() public {
        uint256 huge = type(uint128).max; // 340e36-ish, far above any real cap
        token.mint(owner, huge);
        token.approve(address(vesting), type(uint256).max);

        uint256 id = vesting.createSchedule(beneficiary, huge, START, CLIFF, DURATION, true);
        vm.warp(START + DURATION / 2);
        vesting.revoke(id);
        vm.prank(beneficiary);
        vesting.release(id);
        _assertConservation(id, huge);
    }

    function test_RejectInvalidTimingAndZeroParams() public {
        vm.expectRevert(LinearTokenVesting.InvalidTiming.selector);
        vesting.createSchedule(beneficiary, TOTAL, START, CLIFF, 0, false);
        vm.expectRevert(LinearTokenVesting.InvalidTiming.selector);
        vesting.createSchedule(beneficiary, TOTAL, START, DURATION + 1, DURATION, false);
        vm.expectRevert(LinearTokenVesting.ZeroAmount.selector);
        vesting.createSchedule(beneficiary, 0, START, CLIFF, DURATION, false);
        vm.expectRevert(LinearTokenVesting.ZeroAddress.selector);
        vesting.createSchedule(address(0), TOTAL, START, CLIFF, DURATION, false);
    }

    // ---------------------------------------------------------------------
    // Helpers
    // ---------------------------------------------------------------------

    /// @dev releasedToBeneficiary + refundedToOwner must equal locked total.
    function _assertConservation(uint256 id, uint256 locked) internal {
        LinearTokenVesting.Schedule memory s = vesting.getSchedule(id);
        assertEq(
            s.releasedAmount + s.refundedAmount,
            locked,
            "released + refunded == locked"
        );
        // after the lifecycle the escrow holds nothing for this schedule
        assertEq(token.balanceOf(address(vesting)), 0, "escrow drained");
    }
}
