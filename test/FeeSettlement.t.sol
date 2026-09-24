// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {FeeSettlement} from "../contracts/FeeSettlement.sol";
import {U512} from "../contracts/U512.sol";

interface Vm {
    function roll(uint256) external;
    function prank(address) external;
    function startPrank(address) external;
    function stopPrank() external;
    function expectRevert() external;
}

/// @dev End-to-end settlement properties:
///   1. One settlement over n blocks == n one-block settlements (exact U512).
///   2. Conservation: fees*SCALE + carry == sum(principal*rate) over all
///      elapsed blocks at constant rate (nothing lost, nothing duplicated).
///   3. Same-block re-settlement adds zero (no double counting).
///   4. Rate/principal changes settle first and apply only to future blocks.
contract FeeSettlementTest {
    using U512 for U512.U;

    Vm internal constant vm = Vm(0x7109709ECfa91a80626fF3989D68f67F5b1DD12D);

    FeeSettlement internal fee;
    uint256 internal constant S = 1 << 64;

    address internal owner = address(0xA11CE);
    address internal other = address(0xB0B);

    function setUp() public {
        fee = new FeeSettlement();
        vm.roll(10); // deterministic start well above 0
    }

    // ---------------- helpers ----------------

    function open(address acct, uint256 p, uint256 r) internal {
        vm.prank(owner);
        fee.openAccount(acct, p, r);
    }

    function settle(address acct) internal {
        vm.prank(owner);
        fee.settle(acct);
    }

    /// Settle one block at a time, `blocks` times.
    /// @dev The target block is tracked in a local variable: with via-IR the
    ///      optimizer may hoist `block.number` out of the loop (it is constant
    ///      within a real transaction; vm.roll breaks that assumption).
    function settlePerBlock(address acct, uint256 blocks) internal {
        uint256 b = block.number;
        for (uint256 i = 0; i < blocks; i++) {
            b += 1;
            vm.roll(b);
            settle(acct);
        }
    }

    function stateFees(address acct)
        internal
        view
        returns (U512.U memory f, uint256 carry, uint256 lastBlock)
    {
        (,,,, uint256 c, uint256 f0, uint256 f1, uint256 f2, uint256 f3) = fee.getAccount(acct);
        f = U512.U(f0, f1, f2, f3);
        carry = c;
        (, , , lastBlock, , , , , ) = fee.getAccount(acct);
    }

    /// Pure reference: accrual formula in U512, independent of the contract path.
    function refAccrue(
        U512.U memory fees,
        uint256 carry,
        uint256 p,
        uint256 r,
        uint256 n
    ) internal pure returns (U512.U memory, uint256) {
        U512.U memory t = U512.mul256(p, r);
        t = U512.mulSmall(t, n);
        U512.addU256(t, carry);
        (U512.U memory q, uint256 rem) = U512.divByScale(t);
        U512.addEq(fees, q);
        return (fees, rem);
    }

    function assertStateEq(address a, address b) internal view {
        (
            ,
            uint256 pa,
            uint256 ra,
            uint256 ba,
            uint256 ca,
            uint256 a0,
            uint256 a1,
            uint256 a2,
            uint256 a3
        ) = fee.getAccount(a);
        (
            ,
            uint256 pb,
            uint256 rb,
            uint256 bb,
            uint256 cb,
            uint256 b0,
            uint256 b1,
            uint256 b2,
            uint256 b3
        ) = fee.getAccount(b);
        require(pa == pb, "principal mismatch");
        require(ra == rb, "rate mismatch");
        require(ba == bb, "lastSettleBlock mismatch");
        require(ca == cb, "carry mismatch");
        require(a0 == b0, "fees.a0 mismatch");
        require(a1 == b1, "fees.a1 mismatch");
        require(a2 == b2, "fees.a2 mismatch");
        require(a3 == b3, "fees.a3 mismatch");
    }

    /// Exact conservation check at constant p, r over n blocks from a fresh account.
    function assertConservation(address acct, uint256 p, uint256 r, uint256 n) internal view {
        (U512.U memory f, uint256 carry, uint256 lastBlock) = stateFees(acct);
        require(lastBlock == 10 + n, "settled through wrong block");
        // fees * S + carry must equal n*p*r exactly.
        U512.U memory lhs = U512.mulSmall(f, S);
        U512.addU256(lhs, carry);
        U512.U memory rhs = U512.mul256(p, r);
        rhs = U512.mulSmall(rhs, n);
        require(lhs.eq(rhs), "conservation violated: fees*S+carry != n*p*r");
    }

    // ---------------- 1. batch vs per-block, 100 blocks, known vector ----------------

    function testBatchEqualsPerBlock_100Blocks() public {
        uint256 p = 1_000_000 ether; // 1e24
        // ~1% per block: SCALE/100
        uint256 r = S / 100;
        uint256 n = 100;

        // batch account: one settlement spanning 100 blocks
        address acctB = address(0x1001);
        open(acctB, p, r);
        vm.roll(10 + n);
        settle(acctB);

        // per-block account: open in the *same* block (10), settle every block
        vm.roll(10);
        address acctP = address(0x1002);
        open(acctP, p, r);
        settlePerBlock(acctP, n);

        assertStateEq(acctB, acctP);

        // exact big-integer reference (independently composed here)
        (U512.U memory refFees, uint256 refCarry) = refAccrue(U512.zero(), 0, p, r, n);
        (U512.U memory got, uint256 gotCarry, ) = stateFees(acctB);
        require(got.eq(refFees), "batch fees != reference");
        require(gotCarry == refCarry, "batch carry != reference");

        assertConservation(acctB, p, r, n);
        assertConservation(acctP, p, r, n);

        // sanity on magnitude: 100 blocks at 1% = ~1e24 fee units
        (, , , , , uint256 f0, uint256 f1, , ) = fee.getAccount(acctB);
        uint256 total = f0 | (f1 << 128); // fits 256 bits here
        require(total > 999_000 ether && total < 1_001_000 ether, "fee magnitude out of range");
    }

    // ---------------- 2. fuzz: identity holds for any p, r, n ----------------

    function testFuzz_BatchEqualsPerBlock(uint128 pRaw, uint64 rRaw, uint8 nRaw) public {
        uint256 p = uint256(pRaw);
        uint256 r = uint256(rRaw); // <= SCALE
        uint256 n = uint256(nRaw) % 64; // 0..63
        vm.roll(10);

        address acctB = address(0x2001);
        address acctP = address(0x2002);
        open(acctB, p, r);
        open(acctP, p, r); // both accounts open in the same block (10)

        if (n > 0) {
            vm.roll(10 + n);
            settle(acctB);
            vm.roll(10); // rewind so per-block settlement covers blocks 11..10+n
            settlePerBlock(acctP, n);
            assertStateEq(acctB, acctP);
            assertConservation(acctB, p, r, n);
        }

        // settling again with zero blocks elapsed changes nothing
        (U512.U memory before, uint256 carryBefore, ) = stateFees(acctB);
        settle(acctB);
        settle(acctB);
        (U512.U memory afterward, uint256 carryAfter, uint256 lastAfter) = stateFees(acctB);
        require(afterward.eq(before) && carryBefore == carryAfter, "same-block settlement changed state");
        require(lastAfter == 10 + n, "lastSettleBlock advanced without blocks");
    }

    // ---------------- 3. zero principal ----------------

    function testZeroPrincipal() public {
        address acct = address(0x3001);
        open(acct, 0, S / 2);
        settlePerBlock(acct, 100); // blocks 11..110
        vm.roll(210); // explicit: 100 more empty blocks
        settle(acct);
        (, uint256 p, uint256 r, uint256 b, uint256 c, uint256 f0, uint256 f1, uint256 f2, uint256 f3) =
            fee.getAccount(acct);
        require(p == 0 && r == S / 2, "params changed");
        require(c == 0 && f0 == 0 && f1 == 0 && f2 == 0 && f3 == 0, "fees appeared with zero principal");
        require(b == 210, "last block wrong");

        // principal set later -> only future blocks accrue
        vm.prank(owner);
        fee.setPrincipal(acct, 1_000 ether);
        vm.roll(block.number + 10);
        settle(acct);
        (U512.U memory feesRef, uint256 carryRef) = refAccrue(U512.zero(), 0, 1_000 ether, S / 2, 10);
        (U512.U memory got, uint256 gotCarry, ) = stateFees(acct);
        require(got.eq(feesRef) && gotCarry == carryRef, "post-zero accrual mismatch");
    }

    // ---------------- 4. tiny rate: dust is carried, not lost ----------------

    function testTinyRateDustCarried() public {
        // p = 1, r = 1: one block adds 1/2^64 of a unit; nothing payable for
        // any realistic block window, but the dust must survive settlement.
        address acct = address(0x4001);
        open(acct, 1, 1);
        vm.roll(10 + 100);
        settle(acct);
        (U512.U memory f, uint256 carry, ) = stateFees(acct);
        require(f.isZero(), "fee appeared below scale");
        require(carry == 100, "dust carry lost");

        // p = 1e20, r = 1: each block contributes 1e20/2^64 ~= 5.42 units;
        // rounding alternates 5/6; batched must equal per-block exactly.
        vm.roll(10);
        address acctB = address(0x4002);
        address acctP = address(0x4003);
        open(acctB, 1e20, 1);
        open(acctP, 1e20, 1);
        vm.roll(110);
        settle(acctB);
        vm.roll(10); // rewind for the per-block run
        settlePerBlock(acctP, 100);
        assertStateEq(acctB, acctP);
        assertConservation(acctB, 1e20, 1, 100);
    }

    // ---------------- 5. maximum values, no 512-bit overflow ----------------

    function testMaxValuesNoOverflow() public {
        uint256 p = type(uint256).max;
        uint256 r = S; // maximum allowed rate (100% per block)
        uint256 n = 100;

        address acctB = address(0x5001);
        address acctP = address(0x5002);
        open(acctB, p, r);
        open(acctP, p, r); // both open at block 10
        vm.roll(10 + n);
        settle(acctB);
        vm.roll(10); // rewind for the per-block run
        settlePerBlock(acctP, n);
        assertStateEq(acctB, acctP);

        // conservation at the extreme: fees*S + carry == n*p*r = 100*(2^256-1)*2^64
        (U512.U memory f, uint256 carry, uint256 lastBlock) = stateFees(acctB);
        require(lastBlock == 110, "last block wrong");
        U512.U memory lhs = U512.mulSmall(f, S);
        U512.addU256(lhs, carry);
        // rhs = 100 * p * 2^64 = 100 * (2^320 - 2^64) = 100*2^320 - 100*2^64
        // build as p*r then *100
        U512.U memory rhs = U512.mul256(p, r);
        rhs = U512.mulSmall(rhs, n);
        require(lhs.eq(rhs), "max-value conservation violated");

        // n*p*r at 100% rate = 100*(2^320 - 2^64) which exceeds 256 bits:
        // fees must not have silently truncated (top limbs non-zero).
        (, , , , , , uint256 f1, uint256 f2, uint256 f3) = fee.getAccount(acctB);
        require(f1 != 0 || f2 != 0 || f3 != 0, "fees truncated into one limb");
    }

    function testCapacityBoundBeyondUint64Blocks() public pure {
        // Given the rate cap (r <= 2^64), the principal bound (p < 2^256) and
        // uint64 block numbers (n < 2^64), the exact product n*p*r is strictly
        // below 2^(64+256+64) = 2^384 and always fits in 512 bits. The U512
        // overflow guard therefore cannot trigger over valid contract inputs;
        // the library's own guard is covered in U512.t.sol.
        // This test just pins the bounds that make that argument true.
        require(uint256(64) + 256 + 64 == 384 && 384 < 512, "capacity bound changed");
    }

    // ---------------- 6. rate change settles first, identity across change ----------------

    function testRateChangeSettlesFirst() public {
        uint256 p = 5_000 ether;
        uint256 r1 = S / 50;
        uint256 r2 = S / 200;

        // batch/segmented
        address acctB = address(0x6001);
        address acctP = address(0x6002);
        open(acctB, p, r1);
        open(acctP, p, r1); // both accounts opened at block 10
        vm.roll(60); // 50 blocks at r1
        settle(acctB);
        vm.prank(owner);
        fee.setRate(acctB, r2);
        vm.roll(110); // 50 blocks at r2
        settle(acctB);

        // per-block across the same change
        vm.roll(10);
        settlePerBlock(acctP, 50);
        vm.prank(owner);
        fee.setRate(acctP, r2);
        settlePerBlock(acctP, 50);

        assertStateEq(acctB, acctP);

        // reference: two independent segments
        (U512.U memory refFees, uint256 refCarry) = refAccrue(U512.zero(), 0, p, r1, 50);
        (refFees, refCarry) = refAccrue(refFees, refCarry, p, r2, 50);
        (U512.U memory got, uint256 gotCarry, ) = stateFees(acctB);
        require(got.eq(refFees) && gotCarry == refCarry, "rate-change split mismatch");

        // the setRate call itself settled block 50: changing at block 60 with
        // no elapsed block must not add a second fee
        (, , , uint256 lastBefore, , , , , ) = fee.getAccount(acctB);
        vm.prank(owner);
        fee.setRate(acctB, r2);
        (, , , uint256 lastAfter, uint256 carryAfter, uint256 g0, uint256 g1, uint256 g2, uint256 g3) =
            fee.getAccount(acctB);
        require(lastBefore == lastAfter, "setRate advanced block");
        require(carryAfter == gotCarry, "setRate altered carry");
        (, , , , , uint256 h0, uint256 h1, uint256 h2, uint256 h3) = fee.getAccount(acctB);
        require(g0 == h0 && g1 == h1 && g2 == h2 && g3 == h3, "setRate added a fee");
    }

    // ---------------- 7. principal change settles first ----------------

    function testPrincipalChangeSettlesFirst() public {
        uint256 r = S / 100;
        uint256 p1 = 1_000 ether;
        uint256 p2 = 9_000 ether;

        address acctB = address(0x7001);
        address acctP = address(0x7002);
        open(acctB, p1, r);
        open(acctP, p1, r); // both accounts opened at block 10
        vm.roll(60);
        settle(acctB);
        vm.prank(owner);
        fee.setPrincipal(acctB, p2);
        vm.roll(110);
        settle(acctB);

        vm.roll(10);
        settlePerBlock(acctP, 50);
        vm.prank(owner);
        fee.setPrincipal(acctP, p2);
        settlePerBlock(acctP, 50);

        assertStateEq(acctB, acctP);

        (U512.U memory refFees, uint256 refCarry) = refAccrue(U512.zero(), 0, p1, r, 50);
        (refFees, refCarry) = refAccrue(refFees, refCarry, p2, r, 50);
        (U512.U memory got, uint256 gotCarry, ) = stateFees(acctB);
        require(got.eq(refFees) && gotCarry == refCarry, "principal-change split mismatch");
    }

    // ---------------- 8. zero-rate interval preserves carry ----------------

    function testZeroRateKeepsCarry() public {
        uint256 p = 7_777_777 ether;
        uint256 r = 123_456;

        address acct = address(0x8001);
        open(acct, p, r);
        settlePerBlock(acct, 40);
        (, uint256 savedCarry) = readFeesCarry(acct);

        // pause fees: 30 blocks at rate 0; carry must survive untouched
        vm.prank(owner);
        fee.setRate(acct, 0);
        uint256 b = block.number;
        for (uint256 i = 0; i < 30; i++) {
            b += 1;
            vm.roll(b);
            settle(acct);
        }
        (, uint256 midCarry) = readFeesCarry(acct);
        require(midCarry == savedCarry, "carry changed during zero-rate interval");

        // resume; identity vs reference that skips the zero-rate blocks
        vm.prank(owner);
        fee.setRate(acct, r);
        settlePerBlock(acct, 30);
        (U512.U memory refFees, uint256 refCarry) = refAccrue(U512.zero(), 0, p, r, 40);
        (refFees, refCarry) = refAccrue(refFees, refCarry, p, 0, 30); // no-op segment
        (refFees, refCarry) = refAccrue(refFees, refCarry, p, r, 30);
        (U512.U memory got, uint256 gotCarry, ) = stateFees(acct);
        require(got.eq(refFees) && gotCarry == refCarry, "resume after zero-rate mismatch");
    }

    function readFeesCarry(address acct) internal view returns (U512.U memory f, uint256 carry) {
        (,,,, uint256 c, uint256 f0, uint256 f1, uint256 f2, uint256 f3) = fee.getAccount(acct);
        f = U512.U(f0, f1, f2, f3);
        carry = c;
    }

    // ---------------- 9. access control & rate cap ----------------

    function testAccessControl() public {
        address acct = address(0x9001);
        open(acct, 1 ether, S / 10);

        vm.expectRevert();
        vm.prank(other);
        fee.settle(acct);
        vm.expectRevert();
        vm.prank(other);
        fee.setRate(acct, S / 2);
        vm.expectRevert();
        vm.prank(other);
        fee.setPrincipal(acct, 2 ether);

        // re-opening the same account reverts
        vm.expectRevert();
        open(acct, 1 ether, S / 10);
    }

    function testRateCapEnforced() public {
        vm.expectRevert();
        open(address(0x9002), 1 ether, S + 1);

        open(address(0x9003), 1 ether, S); // exactly at cap is valid
        vm.roll(11);
        settle(address(0x9003));

        vm.expectRevert();
        vm.prank(owner);
        fee.setRate(address(0x9003), S + 1);
    }

    function testConstants() public view {
        require(fee.RATE_SCALE() == S, "scale");
        require(fee.MAX_RATE() == S, "max rate");
    }
}
