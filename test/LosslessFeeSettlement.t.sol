// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
import {LosslessFeeSettlement} from "../src/LosslessFeeSettlement.sol";

/// @dev forge-std is provided by the Foundry toolchain at $FOUNDRY_LIB;
///      `forge test` resolves it without a node/git install.
contract LosslessFeeSettlementTest is Test {
    LosslessFeeSettlement internal c;

    address internal alice = address(0xA11CE);
    address internal bob = address(0xB0B);

    uint256 internal constant SCALE = 1e18;

    function setUp() public {
        c = new LosslessFeeSettlement();
        vm.label(alice, "alice");
        vm.label(bob, "bob");
    }

    /* --------------------------------- helpers -------------------------------- */

    /// @notice Reference settlement: exact integer formula with carry.
    /// @dev Uses 512-bit mulDiv so the reference itself never overflows.
    function _ref(uint256 p, uint256 r, uint256 n, uint256 carry)
        internal
        pure
        returns (uint256 fee, uint256 carryOut)
    {
        uint256 per = Math.mulDiv(p, r, SCALE);
        uint256 rem = mulmod(p, r, SCALE);
        uint256 t = rem * n + carry;
        fee = per * n + t / SCALE;
        carryOut = t % SCALE;
    }

    function _open(address who, uint256 p, uint256 r) internal {
        vm.prank(who);
        c.open(p, r);
    }

    function _settle(address who) internal returns (uint256 fee) {
        vm.prank(who);
        fee = c.settle();
    }

    /* --------------------------------- tests ---------------------------------- */

    /// @notice Acceptance core: settling once across 100 blocks must equal
    ///         settling block-by-block, in BOTH accrued fees and remainder.
    function test_100Blocks_BatchEqualsStepwise() public {
        uint256 p = 1_000e18;
        uint256 r = 0.003e18; // 0.3% per block, chosen to produce dust

        _open(alice, p, r);
        _open(bob, p, r);

        // Stepwise path: settle bob every single block for 100 blocks.
        uint256 stepFee;
        for (uint256 i = 0; i < 100; i++) {
            vm.roll(vm.getBlockNumber() + 1);
            stepFee += _settle(bob);
        }

        // Batch path: alice covers the same 100 blocks in one settle.
        uint256 batchFee = _settle(alice);

        (,,,,, uint256 nbA, uint256 projA, uint256 remA) = c.getAccount(alice);
        (,,,,, uint256 nbB, uint256 projB, uint256 remB) = c.getAccount(bob);

        assertEq(batchFee, stepFee, "fee: batch vs stepwise");
        assertGt(batchFee, 0, "fees should accrue");
        assertEq(remA, remB, "remainder: batch vs stepwise");
        assertEq(nbA, 0);
        assertEq(nbB, 0);
        assertEq(projA, projB);
        assertLt(remA, SCALE, "remainder always < one base unit");

        // Independent big-integer reference.
        (uint256 refFee, uint256 refRem) = _ref(p, r, 100, 0);
        assertEq(batchFee, refFee);
        assertEq(remA, refRem);
    }

    /// @notice Dust that truncates to zero fee per block must not vanish:
    ///         over enough blocks the carried remainder promotes into fees.
    function test_DustIsNeverLost() public {
        // 7 base units * rate 1e17 (10%)/block = 0.7/block -> fee 0, rem 0.7e18
        uint256 p = 7;
        uint256 r = 0.1e18;

        _open(alice, p, r);

        uint256 total;
        for (uint256 i = 0; i < 10; i++) {
            vm.roll(vm.getBlockNumber() + 1);
            total += _settle(alice);
        }
        (,,,, uint256 rem,,,) = c.getAccount(alice);

        // Exact total fee over 10 blocks = floor(7*0.1*10) = 7.
        assertEq(total, 7, "carry must promote dust into fees");
        assertEq(rem, 0);
    }

    /// @notice Calling settle twice in the same block credits nothing twice.
    function test_NoDoubleCounting_SameBlockSettle() public {
        _open(alice, 1_000e18, 0.01e18);
        vm.roll(vm.getBlockNumber() + 5);
        uint256 first = _settle(alice);
        assertGt(first, 0);
        uint256 second = _settle(alice);
        uint256 third = _settle(alice);
        assertEq(second, 0);
        assertEq(third, 0);
        (, uint256 rate, uint256 lb, uint256 accrued, uint256 rem,, uint256 proj,) = c.getAccount(alice);
        assertEq(accrued, first);
        assertEq(lb, block.number);
        assertEq(proj, first);
        rate; rem; // silence
    }

    /// @notice Rate change settles the old regime first; afterwards only the
    ///         new rate applies, and history is not re-priced.
    function test_SetRate_SettlesOldRegimeFirst() public {
        _open(alice, 1_000e18, 0.01e18); // 1%/block
        vm.roll(vm.getBlockNumber() + 10);
        vm.prank(alice);
        c.setRate(0.02e18); // 2%/block

        (, uint256 rateNow, uint256 lb, uint256 accrued, uint256 rem1,, uint256 proj1,) = c.getAccount(alice);
        assertEq(rateNow, 0.02e18);
        assertEq(lb, block.number);
        (uint256 expectedOld, uint256 expectedRem) = _ref(1_000e18, 0.01e18, 10, 0);
        assertEq(accrued, expectedOld);
        assertEq(rem1, expectedRem);
        assertEq(proj1, accrued);

        vm.roll(vm.getBlockNumber() + 10);
        uint256 added = _settle(alice);
        (uint256 expectedNew,) = _ref(1_000e18, 0.02e18, 10, expectedRem);
        assertEq(added, expectedNew);

        (,,,, uint256 rem2,, uint256 proj2,) = c.getAccount(alice);
        assertEq(accrued + added, expectedOld + expectedNew);
        assertEq(rem2, 0);
        assertEq(proj2, accrued + added);
    }

    /// @notice Zero principal accrues nothing yet the account works normally.
    function test_ZeroPrincipal() public {
        _open(alice, 0, 0.05e18);
        vm.roll(vm.getBlockNumber() + 100);
        uint256 fee = _settle(alice);
        assertEq(fee, 0);
        (,,,, uint256 rem,, uint256 proj,) = c.getAccount(alice);
        assertEq(rem, 0);
        assertEq(proj, 0);

        // Growing principal after a dormant period starts clean.
        vm.prank(alice);
        c.setPrincipal(1_000e18);
        vm.roll(vm.getBlockNumber() + 10);
        (uint256 expFee, uint256 expRem) = _ref(1_000e18, 0.05e18, 10, 0);
        assertEq(_settle(alice), expFee);
        (,,,, uint256 rem3,,,) = c.getAccount(alice);
        assertEq(rem3, expRem);
    }

    /// @notice Minimal nonzero rate 1 (1e-18 per block): only carry accumulates
    ///         until enough blocks pass.
    function test_MinimalRate() public {
        uint256 p = 1e9; // per-block fee = 1e9 * 1 / 1e18 = 1e-9 -> pure dust
        uint256 r = 1; // 1e-18 per block
        _open(alice, p, r);
        vm.roll(vm.getBlockNumber() + 999_999_999);
        uint256 fee = _settle(alice);
        (uint256 expFee, uint256 expRem) = _ref(p, r, 999_999_999, 0);
        assertEq(fee, expFee);
        assertEq(fee, 0); // floor(0.999999999)
        (,,,, uint256 rem,,,) = c.getAccount(alice);
        assertEq(rem, expRem);
        assertEq(rem, 999_999_999_000_000_000); // 0.999999999 of one base unit

        vm.roll(vm.getBlockNumber() + 1); // 1e9 blocks total -> exactly 1 base unit
        fee = _settle(alice);
        assertEq(fee, 1);
        (,,,, uint256 rem2,,,) = c.getAccount(alice);
        assertEq(rem2, 0);
    }

    /// @notice Max principal with an ordinary dust-producing rate across 100
    ///         blocks exercises the 512-bit mulDiv path (P*R overflows 256 bits).
    function test_MaxPrincipal_MulDiv512() public {
        uint256 p = type(uint256).max;
        uint256 r = 0.000000001e18; // 1e-9 per block; P*R overflows 256 bits
        _open(alice, p, r);
        vm.roll(vm.getBlockNumber() + 100);
        uint256 fee = _settle(alice);
        (uint256 expFee, uint256 expRem) = _ref(p, r, 100, 0);
        assertEq(fee, expFee);
        (,,,, uint256 rem,,,) = c.getAccount(alice);
        assertEq(rem, expRem);
        assertLt(rem, SCALE);
    }

    /// @notice Max rate (100%/block) with max-ish principal that still fits
    ///         the resulting 256-bit fee: exact accumulation, no remainder.
    function test_MaxRate_ExactAccumulation() public {
        uint256 p = type(uint128).max;
        uint256 r = SCALE; // 100% per block
        _open(alice, p, r);
        vm.roll(vm.getBlockNumber() + 100);
        uint256 fee = _settle(alice);
        assertEq(fee, p * 100);
        (,,,, uint256 rem,,,) = c.getAccount(alice);
        assertEq(rem, 0);
    }

    /// @notice Inputs extreme enough that the correct answer exceeds 256 bits
    ///         must revert rather than wrap.
    function test_Overflow_RevertsNotWraps() public {
        uint256 p = type(uint256).max;
        _open(alice, p, SCALE); // 100%/block
        vm.roll(vm.getBlockNumber() + 2);
        vm.expectRevert();
        vm.prank(alice);
        c.settle();
    }

    /// @notice claim() zeroes the balance: a second claim reverts and later
    ///         fees accrue afresh.
    function test_Claim_ThenNoDoubleSpend() public {
        _open(alice, 500, 0.1e18); // 50/block exact
        vm.roll(vm.getBlockNumber() + 3);
        vm.prank(alice);
        uint256 got = c.claim();
        assertEq(got, 150);
        vm.expectRevert(LosslessFeeSettlement.NothingDue.selector);
        vm.prank(alice);
        c.claim();
        vm.roll(vm.getBlockNumber() + 1);
        vm.prank(alice);
        assertEq(c.claim(), 50);
    }

    /// @notice close() settles through the current block, freezes accrual,
    ///         and blocks reopen.
    function test_Close_SettlesAndFreezes() public {
        _open(alice, 1_000e18, 0.01e18);
        vm.roll(vm.getBlockNumber() + 7);
        vm.prank(alice);
        uint256 finalFee = c.close();
        (uint256 expFee,) = _ref(1_000e18, 0.01e18, 7, 0);
        assertEq(finalFee, expFee);

        vm.roll(vm.getBlockNumber() + 100);
        vm.prank(alice);
        uint256 afterClose = c.settle();
        assertEq(afterClose, 0, "frozen account accrues nothing");

        vm.expectRevert(LosslessFeeSettlement.AlreadyOpen.selector);
        vm.prank(alice);
        c.open(1, 1);
    }

    /// @notice open() rejects rates above the scale.
    function test_Open_RateAboveScale_Reverts() public {
        vm.expectRevert(abi.encodeWithSelector(LosslessFeeSettlement.RateExceedsScale.selector, SCALE + 1));
        c.open(1, SCALE + 1);
    }

    /// @notice settle on an unopened account reverts.
    function test_NotOpen_Reverts() public {
        vm.expectRevert(LosslessFeeSettlement.NotOpen.selector);
        c.settle();
    }

    /// @notice Fuzz: batch(n1+n2) == stepwise(n1)+stepwise(n2) for arbitrary
    ///         (bounded) principals, rates and block counts.
    function testFuzz_BatchEqualsStepwise(uint256 p, uint256 r, uint16 n1, uint16 n2) public {
        p = bound(p, 0, 1e36);
        r = bound(r, 0, SCALE);
        uint256 blocksA = uint256(n1) + 1;
        uint256 blocksB = uint256(n2) + 1;

        _open(alice, p, r);
        _open(bob, p, r);

        // Stepwise path first: bob settles in two chunks.
        vm.roll(vm.getBlockNumber() + blocksA);
        uint256 s1 = _settle(bob);
        vm.roll(vm.getBlockNumber() + blocksB);
        uint256 s2 = _settle(bob);

        // Batch path: alice covers the same blocksA + blocksB in one settle.
        uint256 batchFee = _settle(alice);

        (,,,, uint256 remA,, uint256 projA,) = c.getAccount(alice);
        (,,,, uint256 remB,, uint256 projB,) = c.getAccount(bob);
        assertEq(batchFee, s1 + s2, "fees equal");
        assertEq(remA, remB, "remainders equal");
        assertEq(projA, projB);

        // Rounding-error bound: the settled fee never exceeds the exact real
        // fee, and is short by strictly less than one base unit.
        // exact = (p*r*(n1+n2))/SCALE as an unbounded number; compare via quote:
        (uint256 refFee, uint256 refRem) = _ref(p, r, blocksA + blocksB, 0);
        assertEq(batchFee, refFee);
        assertLt(refRem, SCALE);
    }

    /// @notice Fuzz against the pure quote() view for the carry-in case
    ///         (rate change after prior dust).
    function testFuzz_QuoteMatchesStateful(uint256 p, uint256 r1, uint256 r2, uint16 n16, uint16 m16) public {
        p = bound(p, 0, 1e36);
        r1 = bound(r1, 0, SCALE);
        r2 = bound(r2, 0, SCALE);
        uint256 n = uint256(n16) + 1;
        uint256 m = uint256(m16) + 1;

        _open(alice, p, r1);
        vm.roll(vm.getBlockNumber() + n);
        uint256 f1 = _settle(alice);
        (uint256 expF1, uint256 expRem1) = c.quote(p, r1, n, 0);
        assertEq(f1, expF1);

        vm.prank(alice);
        c.setRate(r2);
        vm.roll(vm.getBlockNumber() + m);
        uint256 f2 = _settle(alice);
        (uint256 expF2, uint256 expRem2) = c.quote(p, r2, m, expRem1);
        assertEq(f2, expF2);

        (,,,, uint256 rem,,,) = c.getAccount(alice);
        assertEq(rem, expRem2);
        assertLt(rem, SCALE);
        // Accounted total is always within one base unit of the exact value
        // of each regime (sub-unit dust lives in the remainder, not lost).
        assertEq(f1 + f2, expF1 + expF2);
    }
}
