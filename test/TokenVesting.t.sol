// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {TokenVesting} from "../src/TokenVesting.sol";
import {SyntheticToken} from "../src/SyntheticToken.sol";

/// @notice On-chain acceptance tests for the linear, cliffed, revocable escrow.
contract TokenVestingTest is Test {
    TokenVesting internal vesting;
    SyntheticToken internal token;

    address internal owner = address(this); // test contract deploys, so it is owner
    address internal beneficiary = address(0xBEEF);
    address internal stranger = address(0xCAFE);

    uint256 internal constant TOK = 1e18;
    uint256 internal constant TOTAL = 1000 * TOK;

    function setUp() public {
        vesting = new TokenVesting();
        token = new SyntheticToken("Synthetic Token", "SYN", 0);
        token.mint(owner, 1_000_000 * TOK);
        token.approve(address(vesting), type(uint256).max);
    }

    // ---- helpers -----------------------------------------------------------

    function _create(uint256 amount, uint256 start, uint256 cliff, uint256 end) internal returns (uint256) {
        return vesting.createSchedule(beneficiary, address(token), amount, start, cliff, end);
    }

    /// createSchedule at t=0: cliff `c`, end `e` seconds after start.
    function _createLinear(uint256 amount, uint256 c, uint256 e) internal returns (uint256) {
        return _create(amount, 0, c, e);
    }

    function _escrowBalance() internal view returns (uint256) {
        return token.balanceOf(address(vesting));
    }

    // ---- acceptance: start/end boundaries ----------------------------------

    function test_BeforeCliffNothingVested() public {
        uint256 id = _createLinear(TOTAL, 100, 200);
        vm.warp(99);
        assertEq(vesting.vestedAmount(id, block.timestamp), 0);
        vm.expectRevert(TokenVesting.NothingToRelease.selector);
        vesting.release(id);

        vm.warp(100);
        // at cliff the beneficiary is exactly at the cliff checkpoint
        assertEq(vesting.vestedAmount(id, block.timestamp), (TOTAL * 100) / 200);
    }

    function test_LinearProgressionAndFullAtEnd() public {
        uint256 id = _createLinear(TOTAL, 100, 200);

        vm.warp(150);
        assertEq(vesting.vestedAmount(id, block.timestamp), (TOTAL * 150) / 200); // 75%

        vm.warp(199);
        assertEq(vesting.vestedAmount(id, block.timestamp), (TOTAL * 199) / 200);

        vm.warp(200);
        assertEq(vesting.vestedAmount(id, block.timestamp), TOTAL);

        // past end stays capped at total
        vm.warp(1000);
        assertEq(vesting.vestedAmount(id, block.timestamp), TOTAL);
    }

    function test_ReleasesOnlyReleasableDelta() public {
        uint256 id = _createLinear(TOTAL, 100, 200);

        vm.warp(150);
        vesting.release(id);
        uint256 firstClaim = (TOTAL * 150) / 200;
        assertEq(token.balanceOf(beneficiary), firstClaim);

        // repeated claim immediately yields nothing
        vm.expectRevert(TokenVesting.NothingToRelease.selector);
        vesting.release(id);

        vm.warp(200);
        vesting.release(id);
        assertEq(token.balanceOf(beneficiary), TOTAL);
        assertEq(_escrowBalance(), 0);
    }

    function test_ReleaseIsPermissionlessButPaysBeneficiaryOnly() public {
        uint256 id = _createLinear(TOTAL, 0, 100);
        vm.warp(50);
        vm.prank(stranger);
        vesting.release(id);
        assertEq(token.balanceOf(beneficiary), (TOTAL * 50) / 100);
        assertEq(token.balanceOf(stranger), 0);
    }

    // ---- acceptance: revocation interleaved with claims --------------------

    function test_RevokeAfterPartialClaimKeepsVestedAndRefundsRest() public {
        // 1000 tokens, cliff 100, end start+300.
        uint256 id = _createLinear(TOTAL, 100, 300);

        // partial claim at t=160 -> vested = total*160/300
        vm.warp(160);
        vesting.release(id);
        uint256 claimed = (TOTAL * 160) / 300;
        assertEq(token.balanceOf(beneficiary), claimed);

        // owner revokes at same instant; vested-at-revoke == claimed, refund = remainder
        uint256 ownerBefore = token.balanceOf(owner);
        vesting.revoke(id);
        uint256 refund = TOTAL - claimed;
        assertEq(token.balanceOf(owner), ownerBefore + refund);

        // curve frozen: later claims cannot pull more than vested-at-revoke
        vm.warp(300);
        vm.expectRevert(TokenVesting.NothingToRelease.selector);
        vesting.release(id);

        // conservation: beneficiary + owner refund == initial lock
        assertEq(token.balanceOf(beneficiary) + refund, TOTAL);
        assertEq(_escrowBalance(), 0);
    }

    function test_RevokeBeforeClaimVestedPortionRemainsClaimable() public {
        // 1000 tokens, cliff 100, end 300; revoke at t=220 (vested = total*220/300).
        uint256 id = _createLinear(TOTAL, 100, 300);

        vm.warp(220);
        vesting.revoke(id);
        uint256 vested = (TOTAL * 220) / 300;
        assertEq(_escrowBalance(), vested); // only vested tokens remain locked

        // time keeps passing but nothing further vests
        vm.warp(299);
        assertEq(vesting.vestedAmount(id, block.timestamp), vested);

        vesting.release(id);
        assertEq(token.balanceOf(beneficiary), vested);
        assertEq(_escrowBalance(), 0);

        // beneficiary vested + owner refund == total
        assertEq(token.balanceOf(beneficiary) + (TOTAL - vested), TOTAL);
    }

    function test_RevokeBeforeCliffRefundsEverything() public {
        uint256 id = _createLinear(TOTAL, 100, 300);
        vm.warp(50);
        uint256 ownerBefore = token.balanceOf(owner);
        vesting.revoke(id);
        assertEq(token.balanceOf(owner), ownerBefore + TOTAL);
        assertEq(_escrowBalance(), 0);

        vm.warp(300);
        vm.expectRevert(TokenVesting.NothingToRelease.selector);
        vesting.release(id);
    }

    function test_RevokeAfterEndRefundsZero() public {
        uint256 id = _createLinear(TOTAL, 100, 300);
        vm.warp(300);
        vesting.revoke(id);
        assertEq(_escrowBalance(), TOTAL); // nothing refunded, all still owed beneficiary
        vesting.release(id);
        assertEq(token.balanceOf(beneficiary), TOTAL);
    }

    // ---- acceptance: non-divisible total (integer dust) --------------------

    function test_NonDivisibleTotalDustSettlesAtEnd() public {
        // 1001 over duration 300: floor division leaves dust until the end.
        uint256 amount = 1001 * TOK;
        uint256 id = _createLinear(amount, 100, 300);

        vm.warp(200);
        uint256 vestedMid = (amount * 200) / 300;
        assertEq(vesting.vestedAmount(id, block.timestamp), vestedMid);
        vesting.release(id);
        assertEq(token.balanceOf(beneficiary), vestedMid);

        vm.warp(300);
        vesting.release(id);
        assertEq(token.balanceOf(beneficiary), amount); // dust delivered at end
        assertEq(_escrowBalance(), 0);
    }

    function test_NonDivisibleRevokeConservesWithDust() public {
        // amount that does not divide evenly at the revoke timestamp
        uint256 amount = 999 * TOK;
        uint256 id = _createLinear(amount, 0, 100);
        vm.warp(33);
        vesting.release(id);
        uint256 vested = (amount * 33) / 100;

        uint256 ownerBefore = token.balanceOf(owner);
        vesting.revoke(id);
        uint256 refund = amount - vested;
        assertEq(token.balanceOf(owner), ownerBefore + refund);

        // already fully claimed the frozen vested amount
        vm.warp(100);
        vm.expectRevert(TokenVesting.NothingToRelease.selector);
        vesting.release(id);

        assertEq(token.balanceOf(beneficiary) + refund, amount);
    }

    // ---- guards & access control ------------------------------------------

    function test_RevertDoubleRevoke() public {
        uint256 id = _createLinear(TOTAL, 100, 300);
        vesting.revoke(id);
        vm.expectRevert(TokenVesting.AlreadyRevoked.selector);
        vesting.revoke(id);
    }

    function test_RevertNonOwnerRevoke() public {
        uint256 id = _createLinear(TOTAL, 100, 300);
        vm.prank(stranger);
        vm.expectRevert(TokenVesting.Unauthorized.selector);
        vesting.revoke(id);
    }

    function test_RevertInvalidTimeline() public {
        vm.expectRevert(TokenVesting.InvalidTimeline.selector);
        vesting.createSchedule(beneficiary, address(token), TOTAL, 200, 100, 300); // cliff < start
        vm.expectRevert(TokenVesting.InvalidTimeline.selector);
        vesting.createSchedule(beneficiary, address(token), TOTAL, 100, 300, 200); // end < cliff
        vm.expectRevert(TokenVesting.InvalidTimeline.selector);
        vesting.createSchedule(beneficiary, address(token), TOTAL, 100, 100, 100); // start == end
    }

    function test_RevertZeroAmountAndBadBeneficiary() public {
        vm.expectRevert(TokenVesting.ZeroAmount.selector);
        vesting.createSchedule(beneficiary, address(token), 0, 0, 0, 100);
        vm.expectRevert(TokenVesting.InvalidBeneficiary.selector);
        vesting.createSchedule(address(0), address(token), TOTAL, 0, 0, 100);
    }

    function test_UnknownScheduleReverts() public {
        vm.expectRevert(TokenVesting.ScheduleNotFound.selector);
        vesting.vestedAmount(424242, block.timestamp);
        vm.expectRevert(TokenVesting.ScheduleNotFound.selector);
        vesting.release(424242);
    }

    // ---- invariant: arbitrary timestamp curve is bounded -------------------

    function testFuzz_VestedBounded(uint256 t) public {
        uint256 id = _createLinear(TOTAL, 100, 300);
        uint256 v = vesting.vestedAmount(id, t);
        assertLe(v, TOTAL);
        if (t < 100) {
            assertEq(v, 0);
        } else if (t >= 300) {
            assertEq(v, TOTAL);
        }
    }
}
