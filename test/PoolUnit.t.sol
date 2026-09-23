// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {Test} from "forge-std/Test.sol";
import {ConstantProductPool} from "../src/ConstantProductPool.sol";
import {TestERC20} from "../src/test/TestERC20.sol";
import {NoReturnToken} from "../src/test/NoReturnToken.sol";
import {FeeOnTransferToken} from "../src/test/FeeOnTransferToken.sol";
import {CPMMMath} from "../src/lib/CPMMMath.sol";

/// @title PoolUnitTest
/// @notice Exact-value unit tests and bounded property tests for the pool.
///         Random multi-step state agreement lives in ReferenceVectorsTest;
///         here every interesting edge is named and asserted independently.
contract PoolUnitTest is Test {
    uint256 internal constant W = 1e18;
    uint256 internal constant MIN_LP = 1000;
    uint256 internal constant FEE_BPS = 30;

    ConstantProductPool internal pool;
    TestERC20 internal t0;
    TestERC20 internal t1;

    address internal alice = makeAddr("alice");
    address internal bob = makeAddr("bob");
    address internal carol = makeAddr("carol");

    uint256 internal deadline;

    function setUp() public {
        t0 = new TestERC20("Token0", "T0");
        t1 = new TestERC20("Token1", "T1");
        pool = new ConstantProductPool(address(t0), address(t1));
        deadline = block.timestamp + 1 days;

        _fund(alice, 1_000_000e18, 1_000_000e18);
        _fund(bob, 1_000_000e18, 1_000_000e18);
        _fund(carol, 1_000_000e18, 1_000_000e18);
    }

    function _fund(address who, uint256 a0, uint256 a1) internal {
        if (a0 != 0) t0.mint(who, a0);
        if (a1 != 0) t1.mint(who, a1);
        vm.startPrank(who);
        t0.approve(address(pool), type(uint256).max);
        t1.approve(address(pool), type(uint256).max);
        vm.stopPrank();
    }

    function _seed(uint256 a0, uint256 a1) internal returns (uint256 shares) {
        vm.prank(alice);
        (,, shares) = pool.addLiquidity(a0, a1, 0, alice, deadline);
    }

    // ==================================================================
    // Construction guards
    // ==================================================================
    function test_Constructor_RejectsSameToken() public {
        vm.expectRevert(ConstantProductPool.SameToken.selector);
        new ConstantProductPool(address(t0), address(t0));
    }

    function test_Constructor_RejectsZeroAddress() public {
        vm.expectRevert(ConstantProductPool.ZeroAddress.selector);
        new ConstantProductPool(address(0), address(t1));
        vm.expectRevert(ConstantProductPool.ZeroAddress.selector);
        new ConstantProductPool(address(t0), address(0));
    }

    function test_Constructor_RejectsPreFundedToken() public {
        // Fund the deterministic CREATE address where a pool is about to be
        // deployed, then deploy through a factory: the constructor must
        // refuse to start with a non-zero token balance.
        PoolDeployer deployer = new PoolDeployer();
        address predicted = vm.computeCreateAddress(address(deployer), vm.getNonce(address(deployer)));
        TestERC20 x = new TestERC20("X", "X");
        TestERC20 y = new TestERC20("Y", "Y");
        x.mint(alice, 1e18);
        vm.prank(alice);
        x.transfer(predicted, 1e18);
        vm.expectRevert(ConstantProductPool.TokenPreFunded.selector);
        deployer.deploy(address(x), address(y));
    }

    // ==================================================================
    // First mint + minimum liquidity lock
    // ==================================================================
    function test_FirstMint_LocksExactMinimumAndMintsGeometricShares() public {
        uint256 a0 = 1000e18;
        uint256 a1 = 1000e18;
        uint256 shares = _seed(a0, a1);

        // raw geometric = sqrt(1000e18 * 1000e18) = 1000e18; lock 1000.
        assertEq(shares, 1000e18 - MIN_LP, "alice shares");
        assertEq(pool.balanceOf(address(0)), MIN_LP, "locked shares");
        assertEq(pool.totalSupply(), 1000e18, "total supply");
        _assertReservesEqualBalances();
        (uint112 r0, uint112 r1, uint256 ts) = pool.getReserves();
        assertEq(uint256(r0), a0);
        assertEq(uint256(r1), a1);
        assertEq(ts, 1000e18);
    }

    function test_FirstMint_SkewedRatio_UsesGeometricMean() public {
        // 400 T0 / 100 T1 -> sqrt(40000) = 200 shares raw, 199 issued.
        uint256 shares = _seed(400e18, 100e18);
        assertEq(shares, 200e18 - MIN_LP);
        assertEq(pool.totalSupply(), 200e18);
        assertEq(pool.balanceOf(address(0)), MIN_LP);
    }

    function test_FirstMint_TooSmall_RevertsNoLiquidityMinted() public {
        // sqrt(999*999) = 999 <= 1000 -> must not mint anything.
        vm.prank(alice);
        vm.expectRevert(ConstantProductPool.NoLiquidityMinted.selector);
        pool.addLiquidity(999, 999, 0, alice, deadline);
        assertEq(pool.totalSupply(), 0, "no shares on failed mint");
        _assertReservesEqualBalances();
    }

    function test_FirstMint_BoundaryExactly1000_Reverts() public {
        vm.prank(alice);
        vm.expectRevert(ConstantProductPool.NoLiquidityMinted.selector);
        pool.addLiquidity(1000, 1000, 0, alice, deadline);
    }

    function test_FirstMint_RejectsZeroAddressRecipient() public {
        vm.prank(alice);
        vm.expectRevert(ConstantProductPool.ZeroRecipient.selector);
        pool.addLiquidity(1000e18, 1000e18, 0, address(0), deadline);
    }

    function test_FirstMint_RejectsPrefunded() public {
        // Sending tokens directly to the pool before the first mint must be
        // caught by the runtime guard (matching the constructor check).
        vm.prank(alice);
        t0.transfer(address(pool), 1e18);
        vm.prank(alice);
        vm.expectRevert(ConstantProductPool.TokenPreFunded.selector);
        pool.addLiquidity(1000e18, 1000e18, 0, alice, deadline);
    }

    function test_FirstMint_RejectsZeroAmounts() public {
        vm.prank(alice);
        vm.expectRevert(ConstantProductPool.ZeroAmount.selector);
        pool.addLiquidity(0, 1000e18, 0, alice, deadline);
        vm.prank(alice);
        vm.expectRevert(ConstantProductPool.ZeroAmount.selector);
        pool.addLiquidity(1000e18, 0, 0, alice, deadline);
    }

    // ==================================================================
    // Deadline / slippage guards
    // ==================================================================
    function test_AnyOp_RevertsAfterDeadline() public {
        _seed(1000e18, 1000e18);
        vm.warp(100);
        vm.prank(bob);
        vm.expectRevert(ConstantProductPool.Expired.selector);
        pool.addLiquidity(1e18, 1e18, 0, bob, 99);
        vm.prank(bob);
        vm.expectRevert(ConstantProductPool.Expired.selector);
        pool.swapExactInput(address(t0), 1e18, 0, bob, 99);
        vm.prank(alice);
        vm.expectRevert(ConstantProductPool.Expired.selector);
        pool.removeLiquidity(1, 0, 0, alice, 99);
    }

    function test_AddLiquidity_MinSharesSlippage_RevertsAtomically() public {
        _seed(1000e18, 1000e18);
        uint256 bal0Before = t0.balanceOf(bob);
        uint256 bal1Before = t1.balanceOf(bob);
        uint256 supplyBefore = pool.totalSupply();

        vm.prank(bob);
        vm.expectRevert(abi.encodeWithSelector(ConstantProductPool.SlippageShares.selector, 1e18, type(uint256).max));
        pool.addLiquidity(1e18, 1e18, type(uint256).max, bob, deadline);

        assertEq(t0.balanceOf(bob), bal0Before, "token0 returned");
        assertEq(t1.balanceOf(bob), bal1Before, "token1 returned");
        assertEq(pool.totalSupply(), supplyBefore, "no shares minted");
        _assertReservesEqualBalances();
    }

    function test_RemoveLiquidity_Slippage_RevertsAtomically() public {
        uint256 shares = _seed(1000e18, 1000e18);
        (,, uint256 tsNow) = pool.getReserves();
        uint256 expect0 = ((shares / 2) * 1000e18) / tsNow;
        vm.prank(alice);
        vm.expectRevert(abi.encodeWithSelector(ConstantProductPool.SlippageToken.selector, expect0, type(uint256).max));
        pool.removeLiquidity(shares / 2, type(uint256).max, 0, alice, deadline);
        assertEq(pool.balanceOf(alice), shares, "shares not burned");
        _assertReservesEqualBalances();
    }

    // ==================================================================
    // Later deposits: proportional amounts + floor share math
    // ==================================================================
    function test_LaterDeposit_PullsNoMoreThanDesired_AndFloorsShares() public {
        _seed(1000e18, 1000e18);
        // Skew the pool with a swap, then bob deposits with a mismatched
        // ratio; the pool pulls <= desired on both sides.
        vm.prank(carol);
        pool.swapExactInput(address(t0), 10e18, 0, carol, deadline);
        (uint112 r0, uint112 r1, uint256 ts) = pool.getReserves();

        uint256 d0 = 5e18;
        uint256 d1 = 9e18;
        uint256 bal0Before = t0.balanceOf(bob);
        uint256 bal1Before = t1.balanceOf(bob);
        vm.prank(bob);
        (uint256 a0, uint256 a1, uint256 sh) = pool.addLiquidity(d0, d1, 0, bob, deadline);

        assertLe(a0, d0, "never over-pulls token0");
        assertLe(a1, d1, "never over-pulls token1");
        assertEq(t0.balanceOf(bob), bal0Before - a0);
        assertEq(t1.balanceOf(bob), bal1Before - a1);

        uint256 s0 = (a0 * ts) / uint256(r0);
        uint256 s1 = (a1 * ts) / uint256(r1);
        assertEq(sh, s0 < s1 ? s0 : s1, "min-floored shares");
        _assertReservesEqualBalances();
    }

    function test_AddRemove_RoundTripKeepsShareProportions() public {
        // Two LPs, a swap moves the price, bob adds and fully exits: his
        // withdrawal must equal what he put in up to floor dust (<=2 wei
        // per token), proving add/remove preserve share proportions.
        _seed(1000e18, 1000e18);
        vm.prank(carol);
        pool.swapExactInput(address(t0), 3e18, 0, carol, deadline);

        uint256 bob0Before = t0.balanceOf(bob);
        uint256 bob1Before = t1.balanceOf(bob);
        vm.prank(bob);
        (uint256 put0, uint256 put1, uint256 sh) = pool.addLiquidity(500e18, 500e18, 0, bob, deadline);

        vm.prank(bob);
        (uint256 got0, uint256 got1) = pool.removeLiquidity(sh, 0, 0, bob, deadline);

        assertEq(pool.balanceOf(bob), 0, "bob fully exited");
        // Floors favour the pool: got <= put, gap bounded by floor dust
        // (one floor per token while reserves are ~1e21 scale).
        assertLe(got0, put0 + 1, "token0 rounding bound");
        assertLe(got1, put1 + 1, "token1 rounding bound");
        assertGe(got0 + 2, put0, "token0 not confiscatory");
        assertGe(got1 + 2, put1, "token1 not confiscatory");
        // Bob's wallet ends within dust of where it started.
        assertApproxEqAbs(t0.balanceOf(bob) - (bob0Before - put0), got0, 2);
        assertApproxEqAbs(t1.balanceOf(bob) - (bob1Before - put1), got1, 2);
        _assertReservesEqualBalances();
    }

    function test_FirstLp_ExitLeavesOnlyLockedDust() public {
        _seed(1000e18, 1000e18);
        uint256 sh = pool.balanceOf(alice);
        vm.prank(alice);
        (uint256 got0, uint256 got1) = pool.removeLiquidity(sh, 0, 0, alice, deadline);
        // Only the MINIMUM_LIQUIDITY lock's slice remains, ~1000 wei each.
        (uint112 r0, uint112 r1, uint256 ts) = pool.getReserves();
        assertEq(ts, MIN_LP, "supply reduced to the permanent lock");
        assertEq(pool.balanceOf(address(0)), MIN_LP, "lock untouched");
        assertLe(uint256(r0), 1100, "dust0 locked");
        assertLe(uint256(r1), 1100, "dust1 locked");
        assertGe(got0, 1000e18 - 1100);
        assertGe(got1, 1000e18 - 1100);
        _assertReservesEqualBalances();
    }

    function test_RemoveLiquidity_RejectsZeroAndOverburn() public {
        uint256 sh = _seed(1000e18, 1000e18);
        vm.prank(alice);
        vm.expectRevert(ConstantProductPool.ZeroAmount.selector);
        pool.removeLiquidity(0, 0, 0, alice, deadline);
        vm.prank(bob);
        vm.expectRevert(ConstantProductPool.InsufficientShares.selector);
        pool.removeLiquidity(1, 0, 0, bob, deadline);
        vm.prank(alice);
        vm.expectRevert(ConstantProductPool.InsufficientShares.selector);
        pool.removeLiquidity(sh + 1, 0, 0, alice, deadline);
    }

    // ==================================================================
    // Swaps: exact amounts, fee, dust, slippage, guards
    // ==================================================================
    function test_Swap_ExactOutputWith03PercentFee() public {
        _seed(1000e18, 1000e18);
        // Exact reference value:
        // b = floor(10e18*997/1000) = 9.97e18
        // out = floor(b*1000e18/(1000e18+b))
        uint256 b = (10e18 * 997) / 1000;
        uint256 expectOut = (b * 1000e18) / (1000e18 + b);
        assertEq(expectOut, 9_871_580_343_970_612_988);

        uint256 t1Before = t1.balanceOf(carol);
        // quote is read against the pristine pre-swap reserves
        assertEq(pool.quoteAmountOut(address(t0), 10e18), expectOut, "quote");
        vm.prank(carol);
        uint256 out = pool.swapExactInput(address(t0), 10e18, 0, carol, deadline);
        assertEq(out, expectOut);
        assertEq(t1.balanceOf(carol), t1Before + expectOut);
        _assertReservesEqualBalances();
    }

    function test_Swap_StrictlyGrowsK() public {
        _seed(1000e18, 1000e18);
        (uint112 r0Before, uint112 r1Before,) = pool.getReserves();
        vm.prank(carol);
        pool.swapExactInput(address(t0), 10e18, 0, carol, deadline);
        (uint112 r0After, uint112 r1After,) = pool.getReserves();
        uint256 kBefore = uint256(r0Before) * uint256(r1Before);
        uint256 kAfter = uint256(r0After) * uint256(r1After);
        assertGt(kAfter, kBefore, "fee must strictly grow k on a real swap");
    }

    function test_Swap_ReverseDirection() public {
        _seed(1000e18, 1000e18);
        vm.prank(carol);
        uint256 out = pool.swapExactInput(address(t1), 7e18, 0, carol, deadline);
        uint256 b = (7e18 * 997) / 1000;
        assertEq(out, (b * 1000e18) / (1000e18 + b));
        _assertReservesEqualBalances();
    }

    function test_Swap_DustInput_RevertsZeroOutput_FeeCannotBeBypassed() public {
        _seed(1000e18, 1000e18);
        // zero input is its own guard
        vm.prank(carol);
        vm.expectRevert(ConstantProductPool.ZeroAmount.selector);
        pool.swapExactInput(address(t0), 0, 0, carol, deadline);

        // Sweep small inputs: whenever the floor formula yields 0 the pool
        // must revert ZeroOutput; when it yields positive the swap succeeds
        // with exactly that output. At this reserve scale the boundary is
        // around input ~ 1e6, so the sweep covers both sides of it.
        uint256[] memory dust = new uint256[](9);
        dust[0] = 1;
        dust[1] = 2;
        dust[2] = 3;
        dust[3] = 1000;
        dust[4] = 1001;
        dust[5] = 1002;
        dust[6] = 1003;
        dust[7] = 1e6 - 1;
        dust[8] = 1e6;
        for (uint256 i = 0; i < dust.length; i++) {
            uint256 inAmt = dust[i];
            uint256 q = pool.quoteAmountOut(address(t0), inAmt);
            uint256 b = (inAmt * 997) / 1000;
            uint256 formula = b == 0 ? 0 : (b * 1000e18) / (1000e18 + b);
            assertEq(q, formula, "quote matches formula");
            if (q == 0) {
                vm.prank(carol);
                vm.expectRevert(ConstantProductPool.ZeroOutput.selector);
                pool.swapExactInput(address(t0), inAmt, 0, carol, deadline);
            } else {
                vm.prank(carol);
                uint256 got = pool.swapExactInput(address(t0), inAmt, 0, carol, deadline);
                assertEq(got, q);
            }
        }
        _assertReservesEqualBalances();
    }

    function test_Swap_MinOutputSlippage_RevertsAtomically() public {
        _seed(1000e18, 1000e18);
        uint256 fair = pool.quoteAmountOut(address(t0), 10e18);
        uint256 bal0Before = t0.balanceOf(carol);
        vm.prank(carol);
        vm.expectRevert(abi.encodeWithSelector(ConstantProductPool.MinOutputNotMet.selector, fair, fair + 1));
        pool.swapExactInput(address(t0), 10e18, fair + 1, carol, deadline);
        assertEq(t0.balanceOf(carol), bal0Before, "input returned");
        _assertReservesEqualBalances();
    }

    function test_Swap_RejectsInvalidTokenAndRecipients() public {
        _seed(1000e18, 1000e18);
        vm.prank(carol);
        vm.expectRevert(ConstantProductPool.InvalidTokenIn.selector);
        pool.swapExactInput(makeAddr("notAToken"), 1e18, 0, carol, deadline);

        vm.prank(carol);
        vm.expectRevert(ConstantProductPool.ZeroRecipient.selector);
        pool.swapExactInput(address(t0), 1e18, 0, address(0), deadline);
        vm.prank(carol);
        vm.expectRevert(ConstantProductPool.ZeroRecipient.selector);
        pool.swapExactInput(address(t0), 1e18, 0, address(pool), deadline);
        vm.prank(carol);
        vm.expectRevert(ConstantProductPool.ZeroRecipient.selector);
        pool.swapExactInput(address(t0), 1e18, 0, address(t0), deadline);
        vm.prank(carol);
        vm.expectRevert(ConstantProductPool.ZeroRecipient.selector);
        pool.swapExactInput(address(t0), 1e18, 0, address(t1), deadline);
    }

    // ==================================================================
    // LP share ERC-20
    // ==================================================================
    function test_LpShares_TransferAndAllowance() public {
        uint256 sh = _seed(1000e18, 1000e18);
        vm.prank(alice);
        pool.transfer(bob, 1234);
        assertEq(pool.balanceOf(bob), 1234);
        assertEq(pool.balanceOf(alice), sh - 1234);

        vm.prank(bob);
        vm.expectRevert(ConstantProductPool.InsufficientShares.selector);
        pool.transfer(alice, 1235);

        vm.prank(bob);
        pool.approve(carol, 1000);
        assertEq(pool.allowance(bob, carol), 1000);
        vm.prank(carol);
        pool.transferFrom(bob, alice, 1000);
        assertEq(pool.allowance(bob, carol), 0);
        assertEq(pool.balanceOf(alice), sh - 234);

        vm.prank(carol);
        vm.expectRevert(ConstantProductPool.InsufficientShares.selector);
        pool.transferFrom(bob, alice, 1);

        vm.prank(alice);
        vm.expectRevert(ConstantProductPool.ZeroAddress.selector);
        pool.transfer(address(0), 1);
    }

    // ==================================================================
    // Token quirks: no-return-value tokens accepted
    // ==================================================================
    function test_NoReturnToken_IsAccepted() public {
        NoReturnToken n0 = new NoReturnToken("N0", "N0");
        NoReturnToken n1 = new NoReturnToken("N1", "N1");
        ConstantProductPool p = new ConstantProductPool(address(n0), address(n1));
        n0.mint(alice, 1000e18);
        n1.mint(alice, 1000e18);
        vm.startPrank(alice);
        n0.approve(address(p), type(uint256).max);
        n1.approve(address(p), type(uint256).max);
        (uint256 a0, uint256 a1, uint256 sh) = p.addLiquidity(1000e18, 1000e18, 0, alice, deadline);
        assertEq(a0, 1000e18);
        assertEq(a1, 1000e18);
        assertEq(sh, 1000e18 - MIN_LP);
        // swap out exercises the no-return-value transfer() path
        n0.mint(bob, 10e18);
        vm.stopPrank();
        vm.startPrank(bob);
        n0.approve(address(p), type(uint256).max);
        uint256 out = p.swapExactInput(address(n0), 10e18, 0, bob, deadline);
        assertGt(out, 0);
        vm.stopPrank();
    }

    // ==================================================================
    // Internal math library direct checks
    // ==================================================================
    function test_Math_Sqrt_FloorForAllSmallValues() public pure {
        for (uint256 i = 0; i < 5000; i++) {
            uint256 z = CPMMMath.sqrt(i);
            assertLe(z * z, i, "sqrt floor lower");
            assertLt(i, (z + 1) * (z + 1), "sqrt floor upper");
        }
        // classic edge values
        assertEq(CPMMMath.sqrt(0), 0);
        assertEq(CPMMMath.sqrt(1), 1);
        assertEq(CPMMMath.sqrt(2), 1);
        assertEq(CPMMMath.sqrt(type(uint256).max), 340282366920938463463374607431768211455);
        assertEq(CPMMMath.sqrt(2 ** 200), 2 ** 100);
        assertEq(CPMMMath.sqrt((2 ** 100) * (2 ** 100) - 1), 2 ** 100 - 1);
    }

    function test_Math_CalcAmountOut_ExplicitFloorFormula() public pure {
        uint256[5] memory ins = [uint256(1), 1000, 1234567, 9e18, 1e24];
        for (uint256 i = 0; i < ins.length; i++) {
            uint256 amountIn = ins[i];
            uint256 b = (amountIn * 997) / 1000;
            uint256 expected = b == 0 ? 0 : (b * 37e18) / (11e18 + b);
            assertEq(CPMMMath.calcAmountOut(amountIn, 11e18, 37e18), expected);
        }
    }

    function test_Math_MintBurn_Rounding() public pure {
        // First deposit
        (uint256 sh, bool first) = CPMMMath.calcMintShares(400e18, 100e18, 0, 0, 0);
        assertTrue(first);
        assertEq(sh, 200e18 - MIN_LP);
        // Later deposit uses floored min
        (sh, first) = CPMMMath.calcMintShares(1e18, 1e18, 1000e18, 1000e18, 1000e18);
        assertFalse(first);
        assertEq(sh, 1e18);
        // Skewed: min side binds
        (sh,) = CPMMMath.calcMintShares(2e18, 1e18, 1000e18, 1000e18, 1000e18);
        assertEq(sh, 1e18);
        // Burn floors both sides
        (uint256 a0, uint256 a1) = CPMMMath.calcBurnAmounts(1, 3, 7, 3);
        assertEq(a0, 1);
        assertEq(a1, 2); // 7/3 floored
    }

    // ==================================================================
    // Bounded property tests
    // ==================================================================
    /// @notice Property: every executed swap strictly grows k and the
    ///         floor output matches the explicit formula (independent
    ///         implementation duplicated here, as the vectors do in Py).
    function testFuzz_Swap_Properties(uint256 amountIn, bool zeroForOne) public {
        _seed(1000e18, 1000e18);
        amountIn = bound(amountIn, 1, 100e18);
        address tin = zeroForOne ? address(t0) : address(t1);
        uint256 quoted = pool.quoteAmountOut(tin, amountIn);
        if (quoted == 0) {
            vm.prank(carol);
            vm.expectRevert(ConstantProductPool.ZeroOutput.selector);
            pool.swapExactInput(tin, amountIn, 0, carol, deadline);
            return;
        }
        (uint112 r0b, uint112 r1b,) = pool.getReserves();
        vm.prank(carol);
        uint256 out = pool.swapExactInput(tin, amountIn, 0, carol, deadline);
        assertEq(out, quoted);
        (uint112 r0a, uint112 r1a,) = pool.getReserves();
        uint256 kb = uint256(r0b) * uint256(r1b);
        uint256 ka = uint256(r0a) * uint256(r1a);
        assertGt(ka, kb, "k strictly grows");

        // explicit independent formula
        uint256 b = (amountIn * 997) / 1000;
        if (zeroForOne) {
            assertEq(out, (b * 1000e18) / (1000e18 + b));
            assertEq(uint256(r0a), 1000e18 + amountIn);
            assertEq(uint256(r1a), 1000e18 - out);
        } else {
            assertEq(out, (b * 1000e18) / (1000e18 + b));
            assertEq(uint256(r1a), 1000e18 + amountIn);
            assertEq(uint256(r0a), 1000e18 - out);
        }
        _assertReservesEqualBalances();
    }

    /// @notice Property: share value is conserved under add+immediate
    ///         remove for a matched-ratio deposit (redeem <= deposit and
    ///         within tight floor dust), and existing LPs cannot be
    ///         diluted by a new deposit (price per share never decreases).
    function testFuzz_AddThenRemove_DoesNotDilute(uint256 d0, uint256 d1) public {
        _seed(1000e18, 1000e18);
        d0 = bound(d0, 1e6, 100e18);
        d1 = bound(d1, 1e6, 100e18);

        uint256 value0Before = _redeemValue0(1e18);
        vm.prank(bob);
        try pool.addLiquidity(d0, d1, 0, bob, deadline) returns (uint256 a0, uint256 a1, uint256 sh) {
            assertGt(sh, 0);
            // Existing LP per-share redemption value cannot decrease.
            assertGe(_redeemValue0(1e18), value0Before, "price-per-share0");

            vm.prank(bob);
            (uint256 got0, uint256 got1) = pool.removeLiquidity(sh, 0, 0, bob, deadline);
            assertLe(got0, a0 + 1);
            assertLe(got1, a1 + 1);
            assertEq(pool.balanceOf(bob), 0);
            _assertReservesEqualBalances();
        } catch (bytes memory err) {
            // Only dust-share rejections (floor -> 0) are acceptable.
            assertEq(bytes4(err), ConstantProductPool.InsufficientShares.selector);
        }
    }

    function _redeemValue0(uint256 sampleShares) internal view returns (uint256) {
        (uint112 r0,, uint256 ts) = pool.getReserves();
        return (sampleShares * uint256(r0)) / ts;
    }

    // ==================================================================
    function _assertReservesEqualBalances() internal view {
        (uint112 r0, uint112 r1,) = pool.getReserves();
        assertEq(t0.balanceOf(address(pool)), uint256(r0), "bal0 == reserve0");
        assertEq(t1.balanceOf(address(pool)), uint256(r1), "bal1 == reserve1");
    }
}

/// @notice Minimal factory used to predict a CREATE address in tests.
contract PoolDeployer {
    function deploy(address a, address b) external returns (ConstantProductPool p) {
        p = new ConstantProductPool(a, b);
    }
}
